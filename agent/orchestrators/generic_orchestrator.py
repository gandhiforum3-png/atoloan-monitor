"""
Generic domain orchestrator.

Drives the consume/debounce/bundle/dispatch loop for ANY domain described by a
``DomainConfig``. The K8s-specific pieces (context fetch, callables, runtime
stream/group keys, urgency/threshold maps) are all injected via ``config`` — this
module imports NO K8s client library (the context fetch is injected via
``config.context_fetcher``).

Flow (per DomainConfig):
    consume config.stream with config.consumer_group
      -> decode each message into InfraEvent(domain=config.domain, ...)
      -> debounce into a SignalBundle(domain=config.domain, ...)
      -> dispatch via config.diagnose / config.remediate / config.run_incident
         (mode="agent" hands the bundle to config.run_incident's tool-use loop).

Confidence routing (mode="diagnoser"):
    human_escalate        always → escalate
    requires_human_review always → escalate
    confidence < config.escalate_below.get(action, 0.80) → escalate
    else                  → remediate

Phase 1 constraint: remediator/agent tools always run in learning_mode=True.
"""

import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Literal

import structlog
from anthropic import AsyncAnthropic
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from agent.registry import DomainConfig
from agent.shared.models import DiagnosisResult, InfraEvent, SignalBundle
from agent.skills.remediators import human_escalator

logger = structlog.get_logger()

CONSUMER_NAME = "atoloan-monitor-1"


def _should_escalate(
    diagnosis: DiagnosisResult, escalate_below: dict[str, float]
) -> tuple[bool, str]:
    """Return (escalate, reason). Routing is identical to the prior k8s logic,
    with the per-action floor map now passed in (config.escalate_below)."""
    if diagnosis.action_type == "human_escalate":
        return True, "Claude determined human intervention is required"

    if diagnosis.requires_human_review:
        return True, "Claude flagged requires_human_review=True"

    threshold = escalate_below.get(diagnosis.action_type, 0.80)
    if diagnosis.confidence < threshold:
        return True, (
            f"confidence {diagnosis.confidence:.2f} < threshold {threshold} "
            f"for action '{diagnosis.action_type}'"
        )

    return False, ""


def _decode_event(msg_id: bytes, fields: dict, domain: str) -> InfraEvent:
    return InfraEvent(
        source=fields[b"source"].decode(),
        domain=domain,
        event_type=fields[b"event_type"].decode(),
        severity=fields[b"severity"].decode(),  # type: ignore[arg-type]
        resource_id=fields[b"resource_id"].decode(),
        timestamp=datetime.fromisoformat(fields[b"timestamp"].decode()),
        raw_payload=json.loads(fields[b"payload"]),
        labels=json.loads(fields[b"labels"]),
        stream_id=msg_id.decode(),
    )


async def _handle_bundle(
    redis: Redis,
    anthropic_client: AsyncAnthropic,
    config: DomainConfig,
    bundle: SignalBundle,
    *,
    learning_mode: bool,
    mode: Literal["diagnoser", "agent"] = "diagnoser",
) -> None:
    incident_id = str(uuid.uuid4())[:8]
    node_names = {s.resource_id for s in bundle.signals}

    logger.info(
        "bundle_received",
        incident_id=incident_id,
        domain=config.domain,
        signal_count=len(bundle.signals),
        resources=list(node_names),
        mode=mode,
    )

    if mode == "agent":
        try:
            await config.run_incident(
                anthropic_client, redis, bundle, incident_id, learning_mode=learning_mode,
            )
        except Exception as exc:
            logger.error("agent_loop_failed", incident_id=incident_id, error=str(exc))
        return

    pods = await config.context_fetcher(node_names) if config.context_fetcher else []

    try:
        diagnosis = await config.diagnose(anthropic_client, bundle, pods)
    except Exception as exc:
        logger.error("diagnoser_failed", incident_id=incident_id, error=str(exc))
        return

    should_esc, reason = _should_escalate(diagnosis, config.escalate_below)

    if should_esc:
        await human_escalator.escalate(
            redis,
            domain=config.domain,
            diagnosis=diagnosis,
            signals=bundle.signals,
            why=reason,
            urgency_map=config.urgency_map,
        )
    else:
        await config.remediate(
            redis,
            incident_id=incident_id,
            diagnosis=diagnosis,
            signals=bundle.signals,
            learning_mode=learning_mode,
        )


async def run(
    redis: Redis,
    anthropic_client: AsyncAnthropic,
    config: DomainConfig,
    *,
    debounce_seconds: int = 30,
    learning_mode: bool = True,
    mode: Literal["diagnoser", "agent"] = "diagnoser",
) -> None:
    """
    Main orchestrator loop for one domain. Runs forever — call from
    asyncio.create_task(). All domain-specific behavior comes from ``config``.

    Args:
        redis:             Connected async Redis client.
        anthropic_client:  Async Anthropic client (reads ANTHROPIC_API_KEY from env).
        config:            DomainConfig describing the domain (stream, group, callables,
                           threshold/urgency maps, optional context fetcher).
        debounce_seconds:  Aggregate signals for this many seconds before diagnosing.
                           Default 30s (prod); use 5s for local testing.
        learning_mode:     If True (Phase 1), remediator/agent tools log but do not act.
        mode:              "diagnoser" (default) for the single-shot diagnoser +
                           remediator pipeline, or "agent" for the tool-use agent loop.
    """
    stream = config.stream
    consumer_group = config.consumer_group

    # Create consumer group — ">" in XREADGROUP means only new messages
    try:
        await redis.xgroup_create(stream, consumer_group, id="$", mkstream=True)
        logger.info("consumer_group_created", stream=stream, group=consumer_group)
    except ResponseError:
        logger.info("consumer_group_exists", stream=stream, group=consumer_group)

    pending: list[InfraEvent] = []
    window_start: datetime | None = None

    logger.info(
        "orchestrator_started",
        domain=config.domain,
        stream=stream,
        debounce_seconds=debounce_seconds,
        learning_mode=learning_mode,
        mode=mode,
    )

    while True:
        # Read new events (block up to 1s)
        entries = await redis.xreadgroup(
            consumer_group,
            CONSUMER_NAME,
            {stream: ">"},
            count=20,
            block=1000,
        )

        for _, messages in (entries or []):
            for msg_id, fields in messages:
                event = _decode_event(msg_id, fields, config.domain)
                pending.append(event)
                if window_start is None:
                    window_start = datetime.now(timezone.utc)
                    logger.info(
                        "debounce_window_opened",
                        first_event=event.event_type,
                        resource=event.resource_id,
                        window_seconds=debounce_seconds,
                    )
                # Ack immediately — we handle replay via incident_id logging
                await redis.xack(stream, consumer_group, msg_id)

        # Flush bundle when debounce window has elapsed
        if pending and window_start:
            elapsed = (datetime.now(timezone.utc) - window_start).total_seconds()
            if elapsed >= debounce_seconds:
                bundle = SignalBundle(
                    domain=config.domain,
                    signals=list(pending),
                    started_at=window_start,
                    window_seconds=debounce_seconds,
                )
                pending.clear()
                window_start = None
                # Handle in background so the consume loop doesn't stall
                asyncio.create_task(
                    _handle_bundle(
                        redis,
                        anthropic_client,
                        config,
                        bundle,
                        learning_mode=learning_mode,
                        mode=mode,
                    )
                )
