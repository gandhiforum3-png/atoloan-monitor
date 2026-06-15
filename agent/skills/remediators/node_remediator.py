"""
Node remediator.

Executes safe remediation actions for K8s node conditions.

Phase 1 (LEARNING_MODE=True): logs what it WOULD do, writes to actions:log
with status="learning_mode_blocked". No K8s mutations.

Phase 3 (LEARNING_MODE=False): executes the action after pre-flight checks
pass:
  - pod_restart           → rolling restart via deployment annotation patch
                             (NOT delete_pod, which is forbidden)
  - deployment_scale_down  → reduce replicas by 1, never below 1
"""

from dataclasses import dataclass
from datetime import datetime, timezone

import structlog
from kubernetes_asyncio import client, config
from kubernetes_asyncio.client import ApiException

from redis.asyncio import Redis

from agent.shared.models import DiagnosisResult, InfraEvent
from agent.shared.safety import FORBIDDEN_OPERATIONS, SafetyViolation, safety_check

logger = structlog.get_logger()

# Per-action confidence thresholds (from REQUIREMENTS.md)
THRESHOLDS: dict[str, float] = {
    "pod_restart": 0.80,
    "deployment_scale_down": 0.85,
    "human_escalate": 0.00,
}

RESTART_ANNOTATION = "kubectl.kubernetes.io/restartedAt"
RESTART_COOLDOWN_SECONDS = 300


@dataclass
class PreflightResult:
    ok: bool
    reason: str = ""


def _meets_threshold(action_type: str, confidence: float) -> bool:
    threshold = THRESHOLDS.get(action_type, 1.0)
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


async def _load_k8s_config() -> None:
    try:
        await config.load_incluster_config()
    except config.ConfigException:
        await config.load_kube_config()


def _parse_target(affected_components: list[str]) -> tuple[str, str] | None:
    """Parse the first 'namespace/deployment' entry from affected_components."""
    for component in affected_components:
        namespace, sep, deployment = component.partition("/")
        namespace = namespace.strip()
        deployment = deployment.strip()
        if sep and namespace and deployment:
            return namespace, deployment
    return None


async def _check_pdb(
    policy_v1: client.PolicyV1Api, namespace: str, deployment: str
) -> tuple[bool, str]:
    """Return (blocked, reason). blocked=True if a matching PDB allows 0 disruptions."""
    try:
        pdbs = await policy_v1.list_namespaced_pod_disruption_budget(namespace)
    except ApiException as exc:
        logger.warning("pdb_check_failed", namespace=namespace, error=str(exc))
        return False, ""

    for pdb in pdbs.items:
        selector = (pdb.spec.selector.match_labels or {}) if pdb.spec.selector else {}
        app = selector.get("app") or selector.get("app.kubernetes.io/name")
        if app != deployment:
            continue
        if (pdb.status.disruption_allowed or 0) == 0:
            return True, f"PDB '{pdb.metadata.name}' allows 0 disruptions"

    return False, ""


async def _preflight_pod_restart(
    apps_v1: client.AppsV1Api,
    policy_v1: client.PolicyV1Api,
    namespace: str,
    deployment: str,
) -> PreflightResult:
    try:
        dep = await apps_v1.read_namespaced_deployment(deployment, namespace)
    except ApiException as exc:
        return PreflightResult(False, f"deployment not found: {exc.reason}")

    annotations = dep.spec.template.metadata.annotations or {}
    last_restart = annotations.get(RESTART_ANNOTATION)
    if last_restart:
        last_dt = datetime.fromisoformat(last_restart)
        elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
        if elapsed < RESTART_COOLDOWN_SECONDS:
            return PreflightResult(
                False, f"cooldown active ({elapsed:.0f}s < {RESTART_COOLDOWN_SECONDS}s)"
            )

    available = dep.status.available_replicas or 0
    if available < 1:
        return PreflightResult(False, f"no available replicas ({available})")

    blocked, reason = await _check_pdb(policy_v1, namespace, deployment)
    if blocked:
        return PreflightResult(False, reason)

    return PreflightResult(True)


async def _preflight_scale_down(
    apps_v1: client.AppsV1Api,
    policy_v1: client.PolicyV1Api,
    namespace: str,
    deployment: str,
) -> tuple[PreflightResult, int]:
    try:
        dep = await apps_v1.read_namespaced_deployment(deployment, namespace)
    except ApiException as exc:
        return PreflightResult(False, f"deployment not found: {exc.reason}"), 0

    current = dep.spec.replicas or 0
    if current < 2:
        return PreflightResult(False, f"already at minimum replicas ({current})"), current

    blocked, reason = await _check_pdb(policy_v1, namespace, deployment)
    if blocked:
        return PreflightResult(False, reason), current

    return PreflightResult(True), current


async def _execute_pod_restart(apps_v1: client.AppsV1Api, namespace: str, deployment: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    patch = {"spec": {"template": {"metadata": {"annotations": {RESTART_ANNOTATION: now}}}}}
    await apps_v1.patch_namespaced_deployment(deployment, namespace, patch)


async def _execute_scale_down(
    apps_v1: client.AppsV1Api, namespace: str, deployment: str, current_replicas: int
) -> int:
    new_replicas = max(1, current_replicas - 1)
    patch = {"spec": {"replicas": new_replicas}}
    await apps_v1.patch_namespaced_deployment(deployment, namespace, patch)
    return new_replicas


async def remediate(
    redis: Redis,
    incident_id: str,
    diagnosis: DiagnosisResult,
    signals: list[InfraEvent],
    *,
    learning_mode: bool = True,
) -> str:
    """
    Attempt remediation based on DiagnosisResult.action_type.

    Returns a status string describing what happened.

    Args:
        redis:          Async Redis client.
        incident_id:    Unique ID for this incident (for audit log correlation).
        diagnosis:      Structured diagnosis from Claude.
        signals:        Raw signals that produced this diagnosis.
        learning_mode:  If True, log the action but do not execute it.
    """
    action = diagnosis.action_type
    confidence = diagnosis.confidence
    affected = ", ".join(diagnosis.affected_components) or "unknown"

    # Hard-stop safety check first — always, regardless of learning_mode
    try:
        safety_check(action, affected)
    except SafetyViolation as exc:
        logger.error("safety_violation_blocked", incident_id=incident_id, action=action, error=str(exc))
        await _log_action(redis, incident_id, "k8s", action, "safety_blocked", confidence, str(exc))
        return "safety_blocked"

    if action == "observe_only":
        logger.info("action_observe_only", incident_id=incident_id, confidence=confidence)
        await _log_action(redis, incident_id, "k8s", action, "observe_only", confidence,
                          "Confidence too low to act; continuing observation")
        return "observe_only"

    if not _meets_threshold(action, confidence):
        # Caller (orchestrator) should have already routed below-threshold to escalator,
        # but guard here defensively.
        detail = f"confidence {confidence:.2f} < threshold {THRESHOLDS.get(action, 1.0)}"
        logger.warning("threshold_not_met", incident_id=incident_id, action=action, detail=detail)
        await _log_action(redis, incident_id, "k8s", action, "threshold_not_met", confidence, detail)
        return "threshold_not_met"

    if learning_mode:
        detail = (
            f"WOULD EXECUTE: {action} on {affected}. "
            f"Recommended: {diagnosis.recommended_action}. "
            f"Confidence: {confidence:.2f}. "
            "Blocked by learning_mode=True (Phase 1)."
        )
        logger.warning(
            "learning_mode_blocked",
            incident_id=incident_id,
            action=action,
            affected=affected,
            confidence=confidence,
        )
        await _log_action(redis, incident_id, "k8s", action, "learning_mode_blocked", confidence, detail)
        return "learning_mode_blocked"

    # --- Real execution ---
    if action not in ("pod_restart", "deployment_scale_down"):
        detail = f"action '{action}' has no execution path"
        logger.error("action_not_implemented", incident_id=incident_id, action=action)
        await _log_action(redis, incident_id, "k8s", action, "not_implemented", confidence, detail)
        return "not_implemented"

    target = _parse_target(diagnosis.affected_components)
    if target is None:
        detail = f"could not parse 'namespace/deployment' from affected_components: {affected}"
        logger.error("target_parse_failed", incident_id=incident_id, affected=affected)
        await _log_action(redis, incident_id, "k8s", action, "target_parse_failed", confidence, detail)
        return "target_parse_failed"

    namespace, deployment = target

    await _load_k8s_config()
    async with client.ApiClient() as api:
        apps_v1 = client.AppsV1Api(api)
        policy_v1 = client.PolicyV1Api(api)

        if action == "pod_restart":
            preflight = await _preflight_pod_restart(apps_v1, policy_v1, namespace, deployment)
            if not preflight.ok:
                detail = f"pre-flight failed for {action} on {namespace}/{deployment}: {preflight.reason}"
                logger.warning("preflight_failed", incident_id=incident_id, action=action, reason=preflight.reason)
                await _log_action(redis, incident_id, "k8s", action, "preflight_failed", confidence, detail)
                return "preflight_failed"

            try:
                await _execute_pod_restart(apps_v1, namespace, deployment)
            except ApiException as exc:
                detail = f"K8s API error during {action} on {namespace}/{deployment}: {exc.reason}"
                logger.error("execution_failed", incident_id=incident_id, action=action, error=str(exc))
                await _log_action(redis, incident_id, "k8s", action, "execution_failed", confidence, detail)
                return "execution_failed"

            detail = f"Rolling restart triggered for {namespace}/{deployment}. {diagnosis.recommended_action}"
            logger.warning(
                "remediation_executed",
                incident_id=incident_id,
                action=action,
                namespace=namespace,
                deployment=deployment,
            )
            await _log_action(redis, incident_id, "k8s", action, "executed", confidence, detail)
            return "executed"

        # action == "deployment_scale_down"
        preflight, current_replicas = await _preflight_scale_down(apps_v1, policy_v1, namespace, deployment)
        if not preflight.ok:
            detail = f"pre-flight failed for {action} on {namespace}/{deployment}: {preflight.reason}"
            logger.warning("preflight_failed", incident_id=incident_id, action=action, reason=preflight.reason)
            await _log_action(redis, incident_id, "k8s", action, "preflight_failed", confidence, detail)
            return "preflight_failed"

        try:
            new_replicas = await _execute_scale_down(apps_v1, namespace, deployment, current_replicas)
        except ApiException as exc:
            detail = f"K8s API error during {action} on {namespace}/{deployment}: {exc.reason}"
            logger.error("execution_failed", incident_id=incident_id, action=action, error=str(exc))
            await _log_action(redis, incident_id, "k8s", action, "execution_failed", confidence, detail)
            return "execution_failed"

        detail = (
            f"Scaled {namespace}/{deployment} from {current_replicas} to {new_replicas} replicas. "
            f"{diagnosis.recommended_action}"
        )
        logger.warning(
            "remediation_executed",
            incident_id=incident_id,
            action=action,
            namespace=namespace,
            deployment=deployment,
            replicas=new_replicas,
        )
        await _log_action(redis, incident_id, "k8s", action, "executed", confidence, detail)
        return "executed"
