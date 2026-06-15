"""
K8s node observer (OBS-02).

Watches Kubernetes node conditions — MemoryPressure, DiskPressure,
PIDPressure, NetworkUnavailable, Ready=False — and publishes InfraEvents
to the Redis Streams event bus. Also detects nodes disappearing from the
API entirely (DELETED watch events) and emits a "node_deleted" event for
those, since that scenario has no pod-level remediation.

Reconnect strategy:
- 410 Gone → resource_version reset to None, immediate re-list
- Network / API errors → exponential backoff with jitter (cap 60s)
- Timeout (300s) → normal reconnect, no backoff
"""

import asyncio
import random
from datetime import datetime, timezone

import structlog
from kubernetes_asyncio import client, config, watch
from kubernetes_asyncio.client import ApiException
from redis.asyncio import Redis

from agent.shared.event_bus import publish
from agent.shared.models import InfraEvent

logger = structlog.get_logger()

# Conditions where status=True means a problem
_TRUE_MEANS_PROBLEM = {"MemoryPressure", "DiskPressure", "PIDPressure", "NetworkUnavailable"}

_CONDITION_EVENT_TYPE = {
    "MemoryPressure": "node_memory_pressure",
    "DiskPressure": "node_disk_pressure",
    "PIDPressure": "node_pid_pressure",
    "NetworkUnavailable": "node_network_unavailable",
    "Ready": "node_not_ready",
}

_CONDITION_SEVERITY = {
    "MemoryPressure": "warning",
    "DiskPressure": "warning",
    "PIDPressure": "warning",
    "NetworkUnavailable": "critical",
    "Ready": "critical",
}


def _is_problem(cond_type: str, status: str) -> bool:
    if cond_type in _TRUE_MEANS_PROBLEM:
        return status == "True"
    if cond_type == "Ready":
        return status != "True"
    return False


def _extract_problems(node) -> list[dict]:
    problems = []
    for cond in node.status.conditions or []:
        if cond.type not in _CONDITION_EVENT_TYPE:
            continue
        if not _is_problem(cond.type, cond.status):
            continue
        problems.append({
            "condition_type": cond.type,
            "status": cond.status,
            "reason": cond.reason or "",
            "message": cond.message or "",
            "last_transition": (
                cond.last_transition_time.isoformat()
                if cond.last_transition_time else None
            ),
        })
    return problems


async def _backoff(attempt: int) -> None:
    delay = min(60.0, (2 ** attempt) + random.uniform(0, 1))
    logger.info("reconnect_backoff", attempt=attempt, delay_seconds=round(delay, 1))
    await asyncio.sleep(delay)


async def _emit(redis: Redis, node_name: str, problem: dict) -> None:
    ctype = problem["condition_type"]
    event = InfraEvent(
        source="k8s-node-observer",
        domain="k8s",
        event_type=_CONDITION_EVENT_TYPE[ctype],
        severity=_CONDITION_SEVERITY[ctype],
        resource_id=node_name,
        timestamp=datetime.now(timezone.utc),
        raw_payload=problem,
        labels={"node": node_name, "condition": ctype},
    )
    stream_id = await publish(redis, event)
    logger.warning(
        "node_condition_detected",
        node=node_name,
        condition=ctype,
        status=problem["status"],
        reason=problem["reason"],
        stream_id=stream_id,
    )


async def _emit_node_deleted(redis: Redis, node_name: str) -> None:
    event = InfraEvent(
        source="k8s-node-observer",
        domain="k8s",
        event_type="node_deleted",
        severity="critical",
        resource_id=node_name,
        timestamp=datetime.now(timezone.utc),
        raw_payload={
            "condition_type": "NodeDeleted",
            "status": "True",
            "reason": "NodeRemovedFromAPI",
            "message": f"Node '{node_name}' was removed from the Kubernetes API (terminated, crashed, or deregistered).",
        },
        labels={"node": node_name, "condition": "NodeDeleted"},
    )
    stream_id = await publish(redis, event)
    logger.error("node_deleted", node=node_name, stream_id=stream_id)


async def watch_nodes(redis: Redis, *, verbose: bool = False) -> None:
    """
    Main entry point. Runs forever — call from asyncio.create_task().

    Args:
        redis:    Connected async Redis client.
        verbose:  Log every node event (not just problems).
    """
    try:
        await config.load_incluster_config()
        logger.info("k8s_auth", source="in-cluster")
    except config.ConfigException:
        await config.load_kube_config()
        logger.info("k8s_auth", source="kubeconfig (local dev)")

    resource_version: str | None = None
    attempt = 0

    while True:
        try:
            async with client.ApiClient() as api:
                v1 = client.CoreV1Api(api)

                # --- Phase 1: initial list to snapshot current node state ---
                if resource_version is None:
                    logger.info("node_list_start")
                    node_list = await v1.list_node()
                    resource_version = node_list.metadata.resource_version
                    logger.info(
                        "node_list_complete",
                        node_count=len(node_list.items),
                        resource_version=resource_version,
                    )
                    for node in node_list.items:
                        name = node.metadata.name
                        problems = _extract_problems(node)
                        if verbose:
                            logger.debug("node_current_state", node=name, problems=len(problems))
                        for p in problems:
                            await _emit(redis, name, p)

                # --- Phase 2: watch for changes from that snapshot ---
                logger.info("watch_start", resource_version=resource_version)
                w = watch.Watch()
                async for event in w.stream(
                    v1.list_node,
                    resource_version=resource_version,
                    timeout_seconds=300,
                ):
                    attempt = 0  # successful event → reset backoff counter
                    etype = event["type"]
                    node = event["object"]
                    resource_version = node.metadata.resource_version
                    node_name = node.metadata.name

                    if verbose:
                        logger.debug("node_watch_event", type=etype, node=node_name)

                    if etype == "DELETED":
                        await _emit_node_deleted(redis, node_name)
                        continue

                    if etype not in ("ADDED", "MODIFIED"):
                        continue

                    for problem in _extract_problems(node):
                        await _emit(redis, node_name, problem)

                # timeout_seconds elapsed — reconnect normally, no backoff
                logger.info("watch_timeout_reconnecting")

        except ApiException as exc:
            if exc.status == 410:
                # resourceVersion too old; K8s compacted history → full re-list
                logger.warning("watch_410_gone", action="resetting_resource_version")
                resource_version = None
                # no backoff on 410 — re-list immediately
            else:
                logger.error("watch_api_error", status=exc.status, reason=exc.reason)
                await _backoff(attempt)
                attempt += 1

        except Exception as exc:
            logger.error("watch_unexpected_error", error=str(exc), exc_info=True)
            await _backoff(attempt)
            attempt += 1
