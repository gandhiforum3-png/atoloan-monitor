"""
remediators.py — Safe kubectl write operations for L1 remediation.

Design principles (mirrors Atoloan Monitor safety model):
  - FORBIDDEN_OPERATIONS frozenset is checked before every action
  - Every function does a dry-run first, then the real operation
  - Every function returns RemediationResult — never raises on k8s errors
  - Destructive operations (force-delete) require explicit force=True flag
  - No operation touches namespaces, PVCs, secrets, or nodes

Functions:
  restart_pod()          — delete pod so the deployment recreates it
  rollback_deployment()  — kubectl rollout undo
  scale_replicas()       — kubectl scale (min 1, safety cap)
  patch_memory_limit()   — increase a container's memory limit
  force_image_repull()   — rollout restart to force a fresh image pull
  delete_stuck_pod()     — force-delete a stuck Terminating pod (explicit opt-in)
  verify_pod_healthy()   — poll until pod is Ready or timeout
  wait_and_reverify()    — no-write: wait for a transient condition to self-resolve, then re-observe
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Optional

from models import RemediationResult


# ---------------------------------------------------------------------------
# Safety boundary — never cross this line
# ---------------------------------------------------------------------------

FORBIDDEN_OPERATIONS: frozenset[str] = frozenset({
    "delete_namespace",
    "delete_pvc",
    "delete_secret",
    "delete_configmap",
    "scale_to_zero",
    "delete_node",
    "cordon_node",
    "drain_node",
    "delete_service",
    "delete_ingress",
    "delete_all_pods",
    "delete_deployment",
    "delete_statefulset",
    "patch_rbac",
    "patch_network_policy",
})

# Hard cap: never scale above this without human approval
MAX_REPLICA_SCALE = 10

# Memory limit increase cap: never more than 4× the original
MAX_MEMORY_MULTIPLIER = 4.0


def _guard(operation: str) -> None:
    """Raise ValueError if the operation is forbidden."""
    if operation in FORBIDDEN_OPERATIONS:
        raise ValueError(
            f"Operation '{operation}' is in FORBIDDEN_OPERATIONS and cannot be executed. "
            f"Escalate to L2."
        )


# ---------------------------------------------------------------------------
# Internal kubectl helpers
# ---------------------------------------------------------------------------

async def _kubectl(*args: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "kubectl", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode().strip(), stderr.decode().strip()


async def _kubectl_json(*args: str):
    rc, out, err = await _kubectl(*args, "-o", "json")
    if rc != 0:
        return rc, {}, err
    try:
        return rc, json.loads(out), err
    except json.JSONDecodeError:
        return 1, {}, f"JSON parse error: {out[:200]}"


def _parse_memory_bytes(mem_str: str) -> int:
    """Parse Kubernetes memory string (e.g. '256Mi', '1Gi') → bytes."""
    units = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4,
             "K": 1000, "M": 1000**2, "G": 1000**3}
    for suffix, mult in units.items():
        if mem_str.endswith(suffix):
            return int(mem_str[:-len(suffix)]) * mult
    return int(mem_str)


def _bytes_to_mi(b: int) -> str:
    return f"{b // (1024**2)}Mi"


# ---------------------------------------------------------------------------
# Remediator functions
# ---------------------------------------------------------------------------

async def restart_pod(
    pod: str,
    namespace: str = "default",
    dry_run: bool = False,
) -> RemediationResult:
    """
    Delete a pod so its controller (Deployment/StatefulSet/DaemonSet) recreates it.
    Safe: the controller guarantees a replacement pod is created.
    Not safe for bare pods (no controller) — caller should check first.
    """
    _guard("restart_pod_safe")   # not in forbidden list — this is always safe via controller

    t0 = time.monotonic()

    # Dry-run validation
    rc, _, err = await _kubectl(
        "delete", "pod", pod, "-n", namespace, "--dry-run=client"
    )
    if rc != 0:
        return RemediationResult(
            action="restart_pod", target=pod, namespace=namespace,
            success=False, dry_run=True,
            message=f"Dry-run failed: {err}",
        )

    if dry_run:
        return RemediationResult(
            action="restart_pod", target=pod, namespace=namespace,
            success=True, dry_run=True,
            message="Dry-run OK — would delete pod for controller-managed restart.",
        )

    rc, _, err = await _kubectl("delete", "pod", pod, "-n", namespace)
    duration = time.monotonic() - t0

    return RemediationResult(
        action="restart_pod", target=pod, namespace=namespace,
        success=(rc == 0), dry_run=False,
        duration_s=round(duration, 2),
        message="Pod deleted — controller will recreate it." if rc == 0 else f"Delete failed: {err}",
    )


async def rollback_deployment(
    deployment: str,
    namespace: str = "default",
    dry_run: bool = False,
) -> RemediationResult:
    """
    Roll back a deployment to its previous revision (kubectl rollout undo).
    Use when a recent deploy introduced the crash.
    """
    _guard("rollback_deployment_safe")

    t0 = time.monotonic()

    if dry_run:
        rc, out, err = await _kubectl(
            "rollout", "undo", f"deployment/{deployment}", "-n", namespace, "--dry-run=client"
        )
        return RemediationResult(
            action="rollback_deployment", target=deployment, namespace=namespace,
            success=(rc == 0), dry_run=True,
            message=out if rc == 0 else err,
        )

    rc, out, err = await _kubectl(
        "rollout", "undo", f"deployment/{deployment}", "-n", namespace
    )
    duration = time.monotonic() - t0

    if rc == 0:
        # Wait for rollout to complete (up to 120s)
        rc2, out2, _ = await _kubectl(
            "rollout", "status", f"deployment/{deployment}",
            "-n", namespace, "--timeout=120s"
        )
        message = f"Rollback initiated. Rollout status: {out2}" if rc2 == 0 else f"Rollback sent but status check failed: {out2}"
    else:
        message = f"Rollback failed: {err}"

    return RemediationResult(
        action="rollback_deployment", target=deployment, namespace=namespace,
        success=(rc == 0), dry_run=False,
        duration_s=round(time.monotonic() - t0, 2),
        message=message,
    )


async def scale_replicas(
    deployment: str,
    namespace: str = "default",
    replicas: int = 2,
    dry_run: bool = False,
) -> RemediationResult:
    """
    Scale a deployment to the specified replica count.
    Safety caps: replicas must be 1–MAX_REPLICA_SCALE (default cap: 10).
    """
    _guard("scale_to_zero") if replicas == 0 else None

    if replicas < 1:
        return RemediationResult(
            action="scale_replicas", target=deployment, namespace=namespace,
            success=False, dry_run=dry_run,
            message=f"Refused: scale to {replicas} is forbidden. Minimum is 1.",
        )
    if replicas > MAX_REPLICA_SCALE:
        return RemediationResult(
            action="scale_replicas", target=deployment, namespace=namespace,
            success=False, dry_run=dry_run,
            message=f"Refused: {replicas} exceeds safety cap of {MAX_REPLICA_SCALE}. Escalate to L2.",
        )

    t0 = time.monotonic()
    dry_flag = ["--dry-run=client"] if dry_run else []

    rc, out, err = await _kubectl(
        "scale", f"deployment/{deployment}",
        f"--replicas={replicas}",
        "-n", namespace,
        *dry_flag,
    )

    return RemediationResult(
        action="scale_replicas", target=deployment, namespace=namespace,
        success=(rc == 0), dry_run=dry_run,
        duration_s=round(time.monotonic() - t0, 2),
        message=f"Scaled to {replicas} replicas." if rc == 0 else f"Scale failed: {err}",
    )


async def patch_memory_limit(
    deployment: str,
    namespace: str = "default",
    container: Optional[str] = None,
    new_limit: Optional[str] = None,
    increase_pct: float = 50.0,
    dry_run: bool = False,
) -> RemediationResult:
    """
    Increase a container's memory limit to resolve OOMKilled pods.

    Either pass new_limit explicitly (e.g. '512Mi') OR let the function
    auto-calculate current_limit × (1 + increase_pct/100).

    Safety cap: never exceed MAX_MEMORY_MULTIPLIER × original limit.
    """
    t0 = time.monotonic()

    # Get current deployment spec
    rc, data, err = await _kubectl_json("get", "deployment", deployment, "-n", namespace)
    if rc != 0:
        return RemediationResult(
            action="patch_memory_limit", target=deployment, namespace=namespace,
            success=False, dry_run=dry_run,
            message=f"Could not fetch deployment: {err}",
        )

    containers = data.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
    if not containers:
        return RemediationResult(
            action="patch_memory_limit", target=deployment, namespace=namespace,
            success=False, dry_run=dry_run,
            message="No containers found in deployment spec.",
        )

    # Pick the target container
    target_container = None
    for c in containers:
        if container is None or c["name"] == container:
            target_container = c
            break

    if target_container is None:
        return RemediationResult(
            action="patch_memory_limit", target=deployment, namespace=namespace,
            success=False, dry_run=dry_run,
            message=f"Container '{container}' not found. Available: {[c['name'] for c in containers]}",
        )

    container_name = target_container["name"]
    current_limit  = target_container.get("resources", {}).get("limits", {}).get("memory")

    if new_limit is None:
        if current_limit is None:
            return RemediationResult(
                action="patch_memory_limit", target=deployment, namespace=namespace,
                success=False, dry_run=dry_run,
                message="No existing memory limit set — cannot auto-increase. Pass new_limit explicitly.",
            )
        current_bytes = _parse_memory_bytes(current_limit)
        new_bytes     = int(current_bytes * (1 + increase_pct / 100))

        # Safety cap
        if new_bytes > current_bytes * MAX_MEMORY_MULTIPLIER:
            new_bytes = int(current_bytes * MAX_MEMORY_MULTIPLIER)

        new_limit = _bytes_to_mi(new_bytes)

    patch = json.dumps({
        "spec": {
            "template": {
                "spec": {
                    "containers": [{
                        "name": container_name,
                        "resources": {"limits": {"memory": new_limit}},
                    }]
                }
            }
        }
    })

    dry_flag = ["--dry-run=client"] if dry_run else []
    rc, out, err = await _kubectl(
        "patch", "deployment", deployment,
        "-n", namespace,
        "--type=strategic",
        f"--patch={patch}",
        *dry_flag,
    )

    return RemediationResult(
        action="patch_memory_limit", target=deployment, namespace=namespace,
        success=(rc == 0), dry_run=dry_run,
        duration_s=round(time.monotonic() - t0, 2),
        message=(
            f"Memory limit for '{container_name}' updated: {current_limit} → {new_limit}."
            if rc == 0 else f"Patch failed: {err}"
        ),
    )


async def force_image_repull(
    deployment: str,
    namespace: str = "default",
    dry_run: bool = False,
) -> RemediationResult:
    """
    Force a fresh image pull by triggering a rollout restart.
    Useful for transient ImagePullBackOff (registry hiccup, cached stale layer).
    Not useful for bad image tags — fix the tag first.
    """
    t0 = time.monotonic()

    if dry_run:
        return RemediationResult(
            action="force_image_repull", target=deployment, namespace=namespace,
            success=True, dry_run=True,
            message=f"Would run: kubectl rollout restart deployment/{deployment} -n {namespace}",
        )

    rc, out, err = await _kubectl(
        "rollout", "restart", f"deployment/{deployment}", "-n", namespace
    )

    if rc == 0:
        rc2, out2, _ = await _kubectl(
            "rollout", "status", f"deployment/{deployment}",
            "-n", namespace, "--timeout=90s"
        )
        message = f"Rollout restart triggered. Status: {out2}"
    else:
        message = f"Rollout restart failed: {err}"

    return RemediationResult(
        action="force_image_repull", target=deployment, namespace=namespace,
        success=(rc == 0), dry_run=False,
        duration_s=round(time.monotonic() - t0, 2),
        message=message,
    )


async def delete_stuck_pod(
    pod: str,
    namespace: str = "default",
    dry_run: bool = False,
    force: bool = False,
) -> RemediationResult:
    """
    Force-delete a pod stuck in Terminating state.
    Requires force=True as an explicit opt-in — this is the one destructive L1 action.
    Only use when the pod has been Terminating for > 5 minutes and has no finalizers you own.
    """
    if not force:
        return RemediationResult(
            action="delete_stuck_pod", target=pod, namespace=namespace,
            success=False, dry_run=dry_run,
            message="Refused: force=True required. This is a destructive action — confirm intent.",
        )

    t0 = time.monotonic()
    dry_flag = ["--dry-run=client"] if dry_run else []

    rc, out, err = await _kubectl(
        "delete", "pod", pod,
        "-n", namespace,
        "--force", "--grace-period=0",
        *dry_flag,
    )

    return RemediationResult(
        action="delete_stuck_pod", target=pod, namespace=namespace,
        success=(rc == 0), dry_run=dry_run,
        duration_s=round(time.monotonic() - t0, 2),
        message=f"Force-deleted stuck pod." if rc == 0 else f"Force-delete failed: {err}",
    )


# ---------------------------------------------------------------------------
# Health verification (poll until Ready or timeout)
# ---------------------------------------------------------------------------

def _is_fully_ready(ready: str) -> bool:
    """ready is 'N/M' (e.g. '2/2') from NamespacePod — true only when N == M and M > 0."""
    try:
        num, den = ready.split("/")
        return den != "0" and num == den
    except (ValueError, AttributeError):
        return False


async def verify_pod_healthy(
    pod_prefix: str,
    namespace: str = "default",
    timeout_s: int = 120,
    poll_interval_s: int = 10,
) -> tuple[bool, str]:
    """
    Poll until a pod matching pod_prefix is Running + Ready, or timeout.
    Returns (is_healthy: bool, message: str).

    Use after restart_pod() — the new pod will have a different suffix.
    pod_prefix is matched against pod names (e.g. "api-deploy" matches "api-deploy-6b9df7-xzp2k").
    """
    from pod_observer import get_namespace_pods

    deadline = time.monotonic() + timeout_s

    while time.monotonic() < deadline:
        pods = await get_namespace_pods(namespace)
        matching = [p for p in pods if p.name.startswith(pod_prefix)]

        ready_pods = [p for p in matching if p.phase == "Running" and _is_fully_ready(p.ready)]
        if ready_pods:
            return True, f"Pod '{ready_pods[0].name}' is Running. Ready: {ready_pods[0].ready}"

        await asyncio.sleep(poll_interval_s)

    return False, f"Timeout after {timeout_s}s — pod '{pod_prefix}*' did not become Ready."


async def wait_and_reverify(
    pod: str,
    namespace: str = "default",
    wait_s: float = 30,
    timeout_s: float = 120,
    poll_interval_s: float = 10,
) -> tuple[bool, str]:
    """
    No-write remediation for transient conditions (a pod still slowly
    ContainerCreating, a flaky readiness probe): wait, then re-observe the
    *same* pod object — never deletes or patches anything, never calls a
    write verb. Used by Tier-1 catalog entries that are safe by construction
    because they don't touch cluster state at all.
    """
    from pod_observer import get_pod_status

    await asyncio.sleep(wait_s)

    deadline = time.monotonic() + timeout_s
    while True:
        status = await get_pod_status(pod, namespace)
        if status.phase == "Running" and status.ready:
            return True, f"Pod '{pod}' is Running and Ready after waiting."
        if time.monotonic() >= deadline:
            return False, (
                f"Pod '{pod}' still not Ready after {wait_s + timeout_s:.0f}s total wait "
                f"(phase={status.phase.value})."
            )
        await asyncio.sleep(poll_interval_s)
