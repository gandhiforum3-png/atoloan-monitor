"""
K8s domain orchestrator.

Consumes raw InfraEvents from the events:k8s Redis Stream, assembles
them into time-windowed SignalBundles, fetches pod context from K8s,
calls the node diagnoser, and routes the DiagnosisResult to either
the human escalator or the node remediator.

Confidence routing (node actions, mode="diagnoser"):
    pod_restart          ≥ 0.80 → remediator  (< 0.80 → escalate)
    deployment_scale_down ≥ 0.85 → remediator (< 0.85 → escalate)
    human_escalate        always → escalator
    observe_only          always → remediator (no-op log)

mode="agent": skips the single-shot diagnoser/remediator pair entirely and
hands the bundle to node_agent's tool-use loop, which investigates, acts,
verifies, and escalates itself (same safety thresholds, enforced per-tool).

Phase 1 constraint: remediator/agent tools always run in learning_mode=True.
"""

import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Literal

import structlog
from anthropic import AsyncAnthropic
from kubernetes_asyncio import client, config
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from agent.shared.models import DiagnosisResult, InfraEvent, SignalBundle
from agent.skills.agents import node_agent
from agent.skills.diagnosers import node_diagnoser
from agent.skills.remediators import human_escalator, node_remediator

logger = structlog.get_logger()

STREAM = "events:k8s"
CONSUMER_GROUP = "k8s-orchestrator"
CONSUMER_NAME = "atoloan-monitor-1"

# Confidence thresholds per action
_ESCALATE_BELOW: dict[str, float] = {
    "pod_restart": 0.80,
    "deployment_scale_down": 0.85,
    "human_escalate": 1.1,  # always escalate
    "observe_only": 1.1,    # handled by remediator as no-op
}


def _should_escalate(diagnosis: DiagnosisResult) -> tuple[bool, str]:
    """Return (escalate, reason)."""
    if diagnosis.action_type == "human_escalate":
        return True, "Claude determined human intervention is required"

    if diagnosis.requires_human_review:
        return True, "Claude flagged requires_human_review=True"

    threshold = _ESCALATE_BELOW.get(diagnosis.action_type, 0.80)
    if diagnosis.confidence < threshold:
        return True, (
            f"confidence {diagnosis.confidence:.2f} < threshold {threshold} "
            f"for action '{diagnosis.action_type}'"
        )

    return False, ""


def _decode_event(msg_id: bytes, fields: dict) -> InfraEvent:
    return InfraEvent(
        source=fields[b"source"].decode(),
        domain="k8s",
        event_type=fields[b"event_type"].decode(),
        severity=fields[b"severity"].decode(),  # type: ignore[arg-type]
        resource_id=fields[b"resource_id"].decode(),
        timestamp=datetime.fromisoformat(fields[b"timestamp"].decode()),
        raw_payload=json.loads(fields[b"payload"]),
        labels=json.loads(fields[b"labels"]),
        stream_id=msg_id.decode(),
    )


async def _fetch_pods_on_nodes(node_names: set[str]) -> list[dict]:
    """List pods currently running on the affected nodes."""
    try:
        await config.load_kube_config()
        async with client.ApiClient() as api:
            v1 = client.CoreV1Api(api)
            pod_list = await v1.list_pod_for_all_namespaces()
            pods = []
            for p in pod_list.items:
                if p.spec.node_name not in node_names:
                    continue
                # Extract memory request from first container
                mem_req = "n/a"
                try:
                    mem_req = p.spec.containers[0].resources.requests.get("memory", "n/a")
                except (AttributeError, TypeError, IndexError):
                    pass
                # Derive deployment name: prefer app label, fall back to pod name prefix
                labels = p.metadata.labels or {}
                deployment = (
                    labels.get("app")
                    or labels.get("app.kubernetes.io/name")
                    or "-".join(p.metadata.name.split("-")[:-2])  # strip replicaset+pod hash
                )
                pods.append({
                    "name": p.metadata.name,
                    "namespace": p.metadata.namespace,
                    "deployment": deployment,
                    "phase": p.status.phase or "Unknown",
                    "node": p.spec.node_name,
                    "memory_request": mem_req,
                })
            return pods
    except Exception as exc:
        logger.warning("pod_context_fetch_failed", error=str(exc))
        return []


async def _handle_bundle(
    redis: Redis,
    anthropic_client: AsyncAnthropic,
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
        signal_count=len(bundle.signals),
        nodes=list(node_names),
        mode=mode,
    )

    if mode == "agent":
        try:
            await node_agent.run_incident(
                anthropic_client, redis, bundle, incident_id, learning_mode=learning_mode,
            )
        except Exception as exc:
            logger.error("agent_loop_failed", incident_id=incident_id, error=str(exc))
        return

    pods = await _fetch_pods_on_nodes(node_names)

    try:
        diagnosis = await node_diagnoser.diagnose(anthropic_client, bundle, pods)
    except Exception as exc:
        logger.error("diagnoser_failed", incident_id=incident_id, error=str(exc))
        return

    should_esc, reason = _should_escalate(diagnosis)

    if should_esc:
        await human_escalator.escalate(
            redis,
            domain="k8s",
            diagnosis=diagnosis,
            signals=bundle.signals,
            why=reason,
        )
    else:
        await node_remediator.remediate(
            redis,
            incident_id=incident_id,
            diagnosis=diagnosis,
            signals=bundle.signals,
            learning_mode=learning_mode,
        )


async def run(
    redis: Redis,
    anthropic_client: AsyncAnthropic,
    *,
    debounce_seconds: int = 30,
    learning_mode: bool = True,
    mode: Literal["diagnoser", "agent"] = "diagnoser",
) -> None:
    """
    Main orchestrator loop. Runs forever — call from asyncio.create_task().

    Args:
        redis:             Connected async Redis client.
        anthropic_client:  Async Anthropic client (reads ANTHROPIC_API_KEY from env).
        debounce_seconds:  Aggregate signals for this many seconds before diagnosing.
                           Default 30s (prod); use 5s for local testing.
        learning_mode:     If True (Phase 1), remediator/agent tools log but do not act.
        mode:              "diagnoser" (default) for the single-shot diagnoser +
                           remediator pipeline, or "agent" for the tool-use agent loop.
    """
    # Create consumer group — ">" in XREADGROUP means only new messages
    try:
        await redis.xgroup_create(STREAM, CONSUMER_GROUP, id="$", mkstream=True)
        logger.info("consumer_group_created", stream=STREAM, group=CONSUMER_GROUP)
    except ResponseError:
        logger.info("consumer_group_exists", stream=STREAM, group=CONSUMER_GROUP)

    pending: list[InfraEvent] = []
    window_start: datetime | None = None

    logger.info(
        "orchestrator_started",
        stream=STREAM,
        debounce_seconds=debounce_seconds,
        learning_mode=learning_mode,
        mode=mode,
    )

    while True:
        # Read new events (block up to 1s)
        entries = await redis.xreadgroup(
            CONSUMER_GROUP,
            CONSUMER_NAME,
            {STREAM: ">"},
            count=20,
            block=1000,
        )

        for _, messages in (entries or []):
            for msg_id, fields in messages:
                event = _decode_event(msg_id, fields)
                pending.append(event)
                if window_start is None:
                    window_start = datetime.now(timezone.utc)
                    logger.info(
                        "debounce_window_opened",
                        first_event=event.event_type,
                        node=event.resource_id,
                        window_seconds=debounce_seconds,
                    )
                # Ack immediately — we handle replay via incident_id logging
                await redis.xack(STREAM, CONSUMER_GROUP, msg_id)

        # Flush bundle when debounce window has elapsed
        if pending and window_start:
            elapsed = (datetime.now(timezone.utc) - window_start).total_seconds()
            if elapsed >= debounce_seconds:
                bundle = SignalBundle(
                    domain="k8s",
                    signals=list(pending),
                    started_at=window_start,
                    window_seconds=debounce_seconds,
                )
                pending.clear()
                window_start = None
                # Handle in background so the consume loop doesn't stall
                asyncio.create_task(
                    _handle_bundle(redis, anthropic_client, bundle, learning_mode=learning_mode, mode=mode)
                )
