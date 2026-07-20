"""
watcher.py — Event-driven pod health watcher.

Holds one persistent connection to the Kubernetes watch API (list_pod_for_all_namespaces
with watch=True) instead of polling every pod on a timer. The apiserver pushes a delta
the moment a pod's status changes, so cost and detection latency scale with the actual
event rate, not with cluster size x poll interval.

On a state transition that looks unhealthy, it runs the L1 runbook in diagnosis-only
mode — no `deployment` is passed, so `l1_runbook`'s remediation branches (restart_pod,
patch_memory_limit, force_image_repull) are never reached. This is intentional: CLAUDE.md
mandates a 7-day observation-only period on first deployment that "cannot be skipped",
so the watcher only diagnoses and records findings. Wiring it to actually remediate is a
deliberate follow-up, not something to flip on implicitly here.

The Kubernetes client's watch stream is a blocking generator, so it runs in a background
thread; each qualifying event is handed back to the FastAPI app's asyncio event loop via
run_coroutine_threadsafe so it can reuse run_l1_runbook and pod_observer's async kubectl
calls unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass

from kubernetes import client, config, watch

from l1_runbook import run_l1_runbook

logger = logging.getLogger("pod-observer.watcher")

COOLDOWN_S     = float(os.environ.get("WATCHER_COOLDOWN_SECONDS", "60"))
HISTORY_MAX    = int(os.environ.get("WATCHER_HISTORY_SIZE", "200"))
WATCH_TIMEOUT_S = 290  # force periodic reconnect; watch streams can go silently stale

# Off by default — see CLAUDE.md's 7-day observation-only constraint. When enabled,
# the watcher resolves each unhealthy pod's owning Deployment and passes it to the L1
# runbook, so OOMKilled/CrashLoop/ImagePull failures get actually remediated instead of
# only escalated. Must be an explicit, recorded override — never the chart default.
ENABLE_REMEDIATION = os.environ.get("WATCHER_ENABLE_REMEDIATION", "false").lower() == "true"

_UNHEALTHY_WAITING_REASONS = {
    "CrashLoopBackOff",
    "ImagePullBackOff",
    "ErrImagePull",
    "CreateContainerConfigError",
    "CreateContainerError",
}


@dataclass
class _PodMemory:
    signature:      str
    last_triggered: float


_memory: dict[str, _PodMemory] = {}
history: deque[dict] = deque(maxlen=HISTORY_MAX)
stats = {
    "events_seen":        0,
    "diagnoses_triggered": 0,
    "started_at":          None,
    "last_event_at":       None,
}

_stop_event = threading.Event()
_thread: threading.Thread | None = None
_apps_v1: client.AppsV1Api | None = None


def _resolve_deployment(pod) -> str | None:
    """
    Walk pod -> ReplicaSet -> Deployment via ownerReferences.
    Returns the owning Deployment name, or None if the pod isn't Deployment-managed
    (e.g. a bare pod or a StatefulSet/DaemonSet) — l1_runbook treats that the same
    as diagnosis-only, since remediation without a Deployment isn't applicable.
    """
    if _apps_v1 is None:
        return None
    owners = pod.metadata.owner_references or []
    rs_owner = next((o for o in owners if o.kind == "ReplicaSet"), None)
    if not rs_owner:
        return None
    try:
        rs = _apps_v1.read_namespaced_replica_set(rs_owner.name, pod.metadata.namespace)
    except Exception:
        logger.exception("watcher: failed to resolve ReplicaSet %s", rs_owner.name)
        return None
    rs_owners = rs.metadata.owner_references or []
    dep_owner = next((o for o in rs_owners if o.kind == "Deployment"), None)
    return dep_owner.name if dep_owner else None


def _signature(pod) -> str:
    """Cheap fingerprint of the parts of pod state worth re-diagnosing on change."""
    status = pod.status
    parts = [status.phase or ""]
    for cs in (status.container_statuses or []):
        waiting    = cs.state.waiting.reason if cs.state and cs.state.waiting else ""
        terminated = cs.state.terminated.reason if cs.state and cs.state.terminated else ""
        parts.append(f"{cs.name}:{waiting}:{terminated}:{cs.restart_count}:{cs.ready}")
    return "|".join(parts)


def _is_unhealthy(pod) -> bool:
    status = pod.status
    if status.phase == "Failed":
        return True
    for cs in (status.container_statuses or []):
        if cs.state and cs.state.waiting and cs.state.waiting.reason in _UNHEALTHY_WAITING_REASONS:
            return True
        if cs.state and cs.state.terminated and cs.state.terminated.reason == "OOMKilled":
            return True
        if status.phase == "Running" and not cs.ready:
            return True
    return False


async def _handle_pod_event(pod) -> None:
    name      = pod.metadata.name
    namespace = pod.metadata.namespace
    key       = f"{namespace}/{name}"

    if not _is_unhealthy(pod):
        _memory.pop(key, None)  # recovered — a future relapse will re-trigger
        return

    sig = _signature(pod)
    mem = _memory.get(key)
    now = time.monotonic()

    if mem and mem.signature == sig and (now - mem.last_triggered) < COOLDOWN_S:
        return  # same failure signature, already diagnosed recently

    _memory[key] = _PodMemory(signature=sig, last_triggered=now)
    stats["diagnoses_triggered"] += 1

    deployment = None
    if ENABLE_REMEDIATION:
        deployment = await asyncio.to_thread(_resolve_deployment, pod)

    logger.info("watcher: diagnosing %s (remediation=%s)", key, bool(deployment))
    try:
        result = await run_l1_runbook(pod=name, namespace=namespace, deployment=deployment)
    except Exception:
        logger.exception("watcher: diagnosis failed for %s", key)
        return

    entry = {
        "pod":           name,
        "namespace":     namespace,
        "detected_at":   time.time(),
        "resolution":    result.resolution.value,
        "severity":      result.severity.value,
        "failure_class": result.failure_class,
        "summary":       result.summary,
    }
    history.appendleft(entry)

    if result.resolution.value != "resolved":
        logger.warning(
            "watcher: %s -> %s [%s] %s",
            key, result.resolution.value, result.severity.value, result.summary,
        )


def _run_watch_thread(loop: asyncio.AbstractEventLoop) -> None:
    global _apps_v1
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()  # local dev fallback (e.g. running outside the cluster)

    v1 = client.CoreV1Api()
    _apps_v1 = client.AppsV1Api()
    stats["started_at"] = time.time()
    backoff = 1.0

    while not _stop_event.is_set():
        w = watch.Watch()
        try:
            for event in w.stream(v1.list_pod_for_all_namespaces, timeout_seconds=WATCH_TIMEOUT_S):
                if _stop_event.is_set():
                    w.stop()
                    break

                stats["events_seen"] += 1
                stats["last_event_at"] = time.time()

                fut = asyncio.run_coroutine_threadsafe(_handle_pod_event(event["object"]), loop)
                fut.result(timeout=180)  # bound worst case, surface exceptions, serialize diagnoses

            backoff = 1.0  # stream ended cleanly (timeout) — reconnect immediately
        except Exception:
            if _stop_event.is_set():
                break
            logger.exception("watcher: stream error, reconnecting in %.0fs", backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


def start() -> None:
    """Start the watch loop in a background thread, bound to the caller's event loop."""
    global _thread
    if _thread is not None:
        return
    loop = asyncio.get_running_loop()
    _stop_event.clear()
    _thread = threading.Thread(target=_run_watch_thread, args=(loop,), daemon=True, name="pod-observer-watcher")
    _thread.start()
    logger.info("watcher: started (cooldown=%ss)", COOLDOWN_S)


def stop() -> None:
    global _thread
    _stop_event.set()
    _thread = None


def status() -> dict:
    return {
        "running":            _thread is not None and _thread.is_alive(),
        "remediation_enabled": ENABLE_REMEDIATION,
        "tracked_pods":       len(_memory),
        **stats,
    }
