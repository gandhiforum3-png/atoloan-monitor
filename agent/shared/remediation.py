"""
Shared remediation primitives — domain-agnostic.

Holds the reusable building blocks every remediation domain (k8s, ec2, db, ...)
needs, with ZERO domain-specific (e.g. K8s API client) imports so a future
domain can register an EC2 reboot threshold without touching K8s code:

  - PreflightResult: result wrapper for pre-flight checks.
  - THRESHOLDS:      per-domain, per-action confidence thresholds.
  - _meets_threshold: domain-aware confidence gate (unknown domain/action -> never met).
  - _log_action:     append a remediation action to the "actions:log" Redis stream.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from redis.asyncio import Redis

# Per-domain, per-action confidence thresholds (from REQUIREMENTS.md).
# Nested so additional domains slot in without touching shared logic, e.g.
#   THRESHOLDS["ec2"]["reboot_instance"] = 0.90
THRESHOLDS: dict[str, dict[str, float]] = {
    "k8s": {
        "pod_restart": 0.80,
        "deployment_scale_down": 0.85,
        "human_escalate": 0.00,
    },
}


@dataclass
class PreflightResult:
    ok: bool
    reason: str = ""


def _meets_threshold(domain: str, action_type: str, confidence: float) -> bool:
    # Two-level default: an unknown domain OR an unknown action both fall through
    # to 1.0 (never met) — preserving the "unknown -> escalate" safety property
    # without ever raising KeyError.
    threshold = THRESHOLDS.get(domain, {}).get(action_type, 1.0)
    return confidence >= threshold


async def _log_action(
    redis: Redis,
    incident_id: str,
    domain: str,
    action: str,
    status: str,
    confidence: float,
    detail: str,
) -> None:
    await redis.xadd(
        "actions:log",
        {
            "incident_id": incident_id,
            "domain": domain,
            "action": action,
            "status": status,
            "confidence": str(confidence),
            "detail": detail,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        maxlen=10_000,
        approximate=True,
    )
