"""
Human escalator.

Called when:
  - diagnosis confidence < per-action threshold, OR
  - action_type == "human_escalate", OR
  - system is in learning mode (Phase 1)

Publishes a HumanEscalationPacket to escalations:{domain} and logs
the recommended action to actions:log so the dashboard can surface it.
"""

import json
import uuid
from datetime import datetime, timezone

import structlog
from redis.asyncio import Redis

from agent.shared.models import DiagnosisResult, HumanEscalationPacket, InfraEvent

logger = structlog.get_logger()

def _derive_urgency(signals: list[InfraEvent], urgency_map: dict[str, str]) -> str:
    """Derive an urgency from signals using a per-domain urgency_map.

    p1_immediate if any signal maps to it; else the first mapped urgency;
    else the conservative "p3_within_1h" fall-through (preserved for unmapped /
    empty signals and an empty urgency_map).
    """
    for s in signals:
        urgency = urgency_map.get(s.event_type)
        if urgency == "p1_immediate":
            return "p1_immediate"
    for s in signals:
        urgency = urgency_map.get(s.event_type)
        if urgency:
            return urgency
    return "p3_within_1h"


async def escalate(
    redis: Redis,
    domain: str,
    diagnosis: DiagnosisResult,
    signals: list[InfraEvent],
    why: str,
    urgency_map: dict[str, str] | None = None,
) -> str:
    """
    Publish escalation packet and return the incident_id.

    Args:
        redis:       Async Redis client.
        domain:      Event domain ("k8s", "db", etc.).
        diagnosis:   Structured diagnosis from Claude.
        signals:     Raw signals that triggered this escalation.
        why:         Human-readable reason for escalating instead of auto-remediating.
        urgency_map: Per-domain event_type -> urgency map. Defaults to {} (an empty
                     map -> everything falls through to p3_within_1h, the safe
                     conservative default for any caller that does not supply one).
    """
    incident_id = str(uuid.uuid4())[:8]
    now = datetime.now(timezone.utc)

    packet = HumanEscalationPacket(
        incident_id=incident_id,
        timestamp=now,
        summary=(
            f"{len(signals)} {domain} signal(s) detected. "
            f"Root cause: {diagnosis.root_cause[:120]}"
        ),
        urgency=_derive_urgency(signals, urgency_map or {}),
        diagnosis=diagnosis,
        why_escalated=why,
        raw_signals=signals,
    )

    # Publish to the escalations stream so dashboard / operator can see it
    escalation_stream = f"escalations:{domain}"
    await redis.xadd(
        escalation_stream,
        {
            "incident_id": incident_id,
            "urgency": packet.urgency,
            "summary": packet.summary,
            "root_cause": diagnosis.root_cause,
            "confidence": str(diagnosis.confidence),
            "recommended_action": diagnosis.recommended_action,
            "action_type": diagnosis.action_type,
            "blast_radius": diagnosis.estimated_blast_radius,
            "why_escalated": why,
            "signal_count": str(len(signals)),
            "timestamp": now.isoformat(),
        },
        maxlen=1000,
        approximate=True,
    )

    # Also append to actions:log (shared audit trail)
    await redis.xadd(
        "actions:log",
        {
            "incident_id": incident_id,
            "domain": domain,
            "action": "human_escalate",
            "status": "escalated",
            "confidence": str(diagnosis.confidence),
            "recommended_action": diagnosis.recommended_action,
            "why": why,
            "timestamp": now.isoformat(),
        },
        maxlen=10_000,
        approximate=True,
    )

    logger.warning(
        "human_escalation",
        incident_id=incident_id,
        urgency=packet.urgency,
        confidence=diagnosis.confidence,
        recommended_action=diagnosis.recommended_action,
        why=why,
    )
    return incident_id
