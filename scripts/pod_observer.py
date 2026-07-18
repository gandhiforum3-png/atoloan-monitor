"""
pod_observer.py — Async Python functions for Kubernetes pod observation.

Each function:
  - Runs kubectl via asyncio subprocess (non-blocking)
  - Returns a typed Pydantic model (never raw strings)
  - Is JSON-serialisable for Claude tool_result payloads
  - Has a sync wrapper (run_sync) for non-async callers

Designed to be used standalone OR as the execution layer behind Claude tool calls.
See k8s_tools.py for the Claude API integration.
"""

from __future__ import annotations

import asyncio
import json
import re
import shlex
from typing import Any, Optional

from models import (
    ContainerResources,
    ContainerStatus,
    ContainerTerminatedState,
    ContainerWaitingState,
    CrashDiagnosis,
    EndpointStatus,
    EventType,
    FailureClass,
    K8sEvent,
    NamespaceHealthReport,
    NamespacePod,
    NodeCondition,
    PendingDiagnosis,
    PodLogs,
    PodPhase,
    PodStatus,
    ResourceRequest,
    ResourceUsage,
    ContainerResources,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _kubectl(*args: str) -> tuple[int, str, str]:
    """
    Run kubectl with the given args. Returns (returncode, stdout, stderr).
    Never raises — callers check returncode.
    """
    cmd = ["kubectl", *args]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode().strip(), stderr.decode().strip()


async def _kubectl_json(*args: str) -> tuple[int, Any, str]:
    """Run kubectl -o json and parse the result. Returns (returncode, parsed_obj, stderr)."""
    rc, out, err = await _kubectl(*args, "-o", "json")
    if rc != 0:
        return rc, {}, err
    try:
        return rc, json.loads(out), err
    except json.JSONDecodeError:
        return 1, {}, f"JSON parse error: {out[:200]}"


def _parse_container_state(state_dict: dict) -> tuple[str, Optional[ContainerWaitingState], Optional[ContainerTerminatedState]]:
    """Parse a .status.containerStatuses[].state dict → (state_name, waiting, terminated)."""
    if "running" in state_dict:
        return "running", None, None
    if "waiting" in state_dict:
        w = state_dict["waiting"]
        return "waiting", ContainerWaitingState(
            reason=w.get("reason"),
            message=w.get("message"),
        ), None
    if "terminated" in state_dict:
        t = state_dict["terminated"]
        return "terminated", None, ContainerTerminatedState(
            exit_code=t.get("exitCode", -1),
            reason=t.get("reason"),
            message=t.get("message"),
            started_at=t.get("startedAt"),
            finished_at=t.get("finishedAt"),
            signal=t.get("signal"),
        )
    return "unknown", None, None


def _parse_last_state(last_state_dict: dict) -> Optional[ContainerTerminatedState]:
    if not last_state_dict or "terminated" not in last_state_dict:
        return None
    t = last_state_dict["terminated"]
    return ContainerTerminatedState(
        exit_code=t.get("exitCode", -1),
        reason=t.get("reason"),
        message=t.get("message"),
        started_at=t.get("startedAt"),
        finished_at=t.get("finishedAt"),
        signal=t.get("signal"),
    )


def _parse_container_statuses(raw_statuses: list[dict]) -> list[ContainerStatus]:
    result = []
    for cs in raw_statuses:
        state_name, waiting, terminated = _parse_container_state(cs.get("state", {}))
        last_state = _parse_last_state(cs.get("lastState", {}))
        result.append(ContainerStatus(
            name=cs["name"],
            ready=cs.get("ready", False),
            restart_count=cs.get("restartCount", 0),
            image=cs.get("image", ""),
            state=state_name,
            waiting=waiting,
            terminated=terminated,
            last_state=last_state,
        ))
    return result


def _events_from_json(items: list[dict], filter_type: Optional[str] = None) -> list[K8sEvent]:
    events = []
    for ev in items:
        etype = ev.get("type", "Normal")
        if filter_type and etype != filter_type:
            continue
        events.append(K8sEvent(
            type=EventType.WARNING if etype == "Warning" else EventType.NORMAL,
            reason=ev.get("reason", ""),
            message=ev.get("message", ""),
            count=ev.get("count", 1),
            first_time=ev.get("firstTimestamp") or ev.get("eventTime"),
            last_time=ev.get("lastTimestamp"),
            component=ev.get("source", {}).get("component"),
        ))
    # newest first
    events.sort(key=lambda e: e.last_time or "", reverse=True)
    return events


def _infer_failure_hint(container_statuses: list[ContainerStatus]) -> Optional[str]:
    for cs in container_statuses:
        if cs.waiting and cs.waiting.reason:
            return cs.waiting.reason
        if cs.last_state and cs.last_state.reason:
            return cs.last_state.reason
    return None


# ---------------------------------------------------------------------------
# Public observer functions
# ---------------------------------------------------------------------------

async def get_pod_status(pod: str, namespace: str = "default") -> PodStatus:
    """
    Return the full status of a single pod: phase, conditions, container states,
    restart counts, and a failure hint.
    """
    rc, data, err = await _kubectl_json("get", "pod", pod, "-n", namespace)
    if rc != 0:
        return PodStatus(
            pod=pod, namespace=namespace, phase=PodPhase.UNKNOWN,
            failure_hint=f"kubectl error: {err}",
        )

    status = data.get("status", {})
    spec   = data.get("spec", {})
    meta   = data.get("metadata", {})

    phase_raw = status.get("phase", "Unknown")
    try:
        phase = PodPhase(phase_raw)
    except ValueError:
        phase = PodPhase.UNKNOWN

    # Conditions → dict
    conditions = {
        c["type"]: c["status"]
        for c in status.get("conditions", [])
    }

    container_statuses      = _parse_container_statuses(status.get("containerStatuses", []))
    init_container_statuses = _parse_container_statuses(status.get("initContainerStatuses", []))

    total_restarts = sum(cs.restart_count for cs in container_statuses)
    ready = conditions.get("Ready", "False") == "True"

    failure_hint = _infer_failure_hint(container_statuses) if not ready else None

    return PodStatus(
        pod=pod,
        namespace=namespace,
        phase=phase,
        node=spec.get("nodeName"),
        pod_ip=status.get("podIP"),
        ready=ready,
        total_restarts=total_restarts,
        container_statuses=container_statuses,
        init_container_statuses=init_container_statuses,
        conditions=conditions,
        start_time=status.get("startTime"),
        failure_hint=failure_hint,
    )


async def get_pod_logs(
    pod: str,
    namespace: str = "default",
    container: Optional[str] = None,
    previous: bool = False,
    tail: int = 100,
) -> PodLogs:
    """
    Fetch pod logs. Set previous=True to get the last crashed container's output.
    Returns structured PodLogs with lines as a list.
    """
    args = ["logs", pod, "-n", namespace, f"--tail={tail}"]
    if container:
        args += ["-c", container]
    if previous:
        args += ["--previous"]

    rc, out, err = await _kubectl(*args)

    # If no container specified, figure out the first one
    resolved_container = container or pod  # best effort

    if rc != 0:
        return PodLogs(
            pod=pod, namespace=namespace,
            container=resolved_container,
            previous=previous,
            lines=[f"[error] {err}"],
        )

    lines = out.splitlines()
    return PodLogs(
        pod=pod,
        namespace=namespace,
        container=resolved_container,
        previous=previous,
        lines=lines,
        truncated=len(lines) >= tail,
    )


async def get_pod_events(pod: str, namespace: str = "default") -> list[K8sEvent]:
    """
    Return all events involving this pod, sorted newest first.
    """
    rc, data, _ = await _kubectl_json(
        "get", "events", "-n", namespace,
        f"--field-selector=involvedObject.name={pod}",
    )
    if rc != 0 or not data:
        return []
    return _events_from_json(data.get("items", []))


async def get_namespace_pods(
    namespace: str = "default",
    all_namespaces: bool = False,
) -> list[NamespacePod]:
    """
    List all pods in a namespace (or cluster-wide). Returns structured summaries.
    """
    args = ["get", "pods"]
    if all_namespaces:
        args += ["-A"]
    else:
        args += ["-n", namespace]

    rc, data, _ = await _kubectl_json(*args)
    if rc != 0:
        return []

    pods = []
    for item in data.get("items", []):
        meta   = item.get("metadata", {})
        status = item.get("status", {})
        container_statuses = status.get("containerStatuses", [])

        ready_count = sum(1 for cs in container_statuses if cs.get("ready"))
        total_count = len(container_statuses)
        restarts    = sum(cs.get("restartCount", 0) for cs in container_statuses)

        pods.append(NamespacePod(
            name=meta.get("name", ""),
            namespace=meta.get("namespace", namespace),
            phase=status.get("phase", "Unknown"),
            ready=f"{ready_count}/{total_count}",
            restarts=restarts,
            node=item.get("spec", {}).get("nodeName"),
        ))

    return pods


async def get_namespace_events(
    namespace: str = "default",
    warnings_only: bool = True,
    limit: int = 30,
) -> list[K8sEvent]:
    """
    Fetch events from a namespace. Set warnings_only=True (default) for fast triage.
    """
    rc, data, _ = await _kubectl_json("get", "events", "-n", namespace)
    if rc != 0:
        return []

    filter_type = "Warning" if warnings_only else None
    events = _events_from_json(data.get("items", []), filter_type=filter_type)
    return events[:limit]


async def get_pod_resource_usage(pod: str, namespace: str = "default") -> ResourceUsage:
    """
    Get live CPU + memory usage from metrics-server.
    If metrics-server is unavailable, returns the limit/request config only.
    """
    # First get requests/limits from the pod spec
    rc_spec, spec_data, _ = await _kubectl_json("get", "pod", pod, "-n", namespace)
    containers_config: list[ContainerResources] = []

    if rc_spec == 0:
        for c in spec_data.get("spec", {}).get("containers", []):
            res = c.get("resources", {})
            req = res.get("requests", {})
            lim = res.get("limits", {})
            containers_config.append(ContainerResources(
                container=c["name"],
                requests=ResourceRequest(cpu=req.get("cpu"), memory=req.get("memory")),
                limits=ResourceRequest(cpu=lim.get("cpu"), memory=lim.get("memory")),
            ))

    # Try metrics-server
    rc_top, top_out, _ = await _kubectl("top", "pod", pod, "-n", namespace, "--containers", "--no-headers")
    if rc_top == 0:
        # Parse "pod  container  cpu  memory"
        usage_map: dict[str, tuple[str, str]] = {}
        for line in top_out.splitlines():
            parts = line.split()
            if len(parts) >= 4:
                usage_map[parts[1]] = (parts[2], parts[3])

        for cr in containers_config:
            if cr.container in usage_map:
                cr.cpu_usage, cr.memory_usage = usage_map[cr.container]

        return ResourceUsage(pod=pod, namespace=namespace, containers=containers_config)

    return ResourceUsage(
        pod=pod, namespace=namespace,
        containers=containers_config,
        metrics_unavailable=True,
    )


async def get_node_conditions(pressure_only: bool = True) -> list[NodeCondition]:
    """
    Return node conditions. Set pressure_only=True to surface MemoryPressure /
    DiskPressure / PIDPressure / NotReady nodes only.
    """
    rc, data, _ = await _kubectl_json("get", "nodes")
    if rc != 0:
        return []

    results = []
    for node in data.get("items", []):
        name = node["metadata"]["name"]
        for cond in node.get("status", {}).get("conditions", []):
            ctype  = cond.get("type", "")
            status = cond.get("status", "Unknown")

            # Pressure conditions are bad when True; Ready is bad when not True
            is_bad = (
                (ctype in ("MemoryPressure", "DiskPressure", "PIDPressure") and status == "True")
                or (ctype == "Ready" and status != "True")
            )

            if pressure_only and not is_bad:
                continue

            results.append(NodeCondition(
                node=name,
                condition=ctype,
                status=status,
                reason=cond.get("reason"),
                message=cond.get("message"),
            ))

    return results


async def get_endpoints(service: str, namespace: str = "default") -> EndpointStatus:
    """
    Check whether a Service has ready endpoint addresses (i.e., pods are actually receiving traffic).
    """
    rc, data, err = await _kubectl_json("get", "endpoints", service, "-n", namespace)
    if rc != 0:
        return EndpointStatus(service=service, namespace=namespace)

    ready: list[str]     = []
    not_ready: list[str] = []

    for subset in data.get("subsets", []):
        port = subset.get("ports", [{}])[0].get("port", "")
        for addr in subset.get("addresses", []):
            ready.append(f"{addr.get('ip')}:{port}")
        for addr in subset.get("notReadyAddresses", []):
            not_ready.append(f"{addr.get('ip')}:{port}")

    # Get selector from the Service itself
    rc_svc, svc_data, _ = await _kubectl_json("get", "service", service, "-n", namespace)
    selector = {}
    if rc_svc == 0:
        selector = svc_data.get("spec", {}).get("selector", {})

    return EndpointStatus(
        service=service,
        namespace=namespace,
        ready_addresses=ready,
        not_ready_addresses=not_ready,
        selector=selector,
    )


# ---------------------------------------------------------------------------
# Composite diagnostic functions
# ---------------------------------------------------------------------------

async def diagnose_crash(pod: str, namespace: str = "default") -> CrashDiagnosis:
    """
    Full crash diagnosis: exit code, kill reason, previous logs, recent events.
    This is the primary function for CrashLoopBackOff / OOMKilled investigations.
    """
    # Gather in parallel
    status_task   = asyncio.create_task(get_pod_status(pod, namespace))
    events_task   = asyncio.create_task(get_pod_events(pod, namespace))
    prev_log_task = asyncio.create_task(get_pod_logs(pod, namespace, previous=True, tail=100))
    usage_task    = asyncio.create_task(get_pod_resource_usage(pod, namespace))

    status   = await status_task
    events   = await events_task
    prev_log = await prev_log_task
    usage    = await usage_task

    # Determine failure class, exit code, kill reason
    failure_class = FailureClass.UNKNOWN
    exit_code: Optional[int] = None
    kill_reason: Optional[str] = None
    memory_limit: Optional[str] = None
    memory_usage: Optional[str] = None

    for cs in status.container_statuses:
        # Waiting state → CrashLoopBackOff
        if cs.waiting and cs.waiting.reason == "CrashLoopBackOff":
            failure_class = FailureClass.CRASH_LOOP

        # Last terminated state → real exit info
        if cs.last_state:
            ls = cs.last_state
            exit_code   = ls.exit_code
            kill_reason = ls.reason
            if ls.reason == "OOMKilled":
                failure_class = FailureClass.OOM_KILLED

    # Map memory from resource usage
    if usage.containers:
        c = usage.containers[0]
        memory_limit = c.limits.memory
        memory_usage = c.memory_usage

    # Summary
    if failure_class == FailureClass.OOM_KILLED:
        summary = (
            f"OOMKilled (exit 137) — container exceeded memory limit "
            f"({memory_limit or 'unset'}). "
            f"{'Live usage: ' + memory_usage + '.' if memory_usage else ''} "
            f"Increase resources.limits.memory or fix the memory leak."
        )
    elif failure_class == FailureClass.CRASH_LOOP:
        log_tail = prev_log.lines[-5:] if prev_log.lines else []
        summary = (
            f"CrashLoopBackOff — exit {exit_code}, reason: {kill_reason or 'unknown'}. "
            f"Last log lines: {' | '.join(log_tail)}"
        )
    else:
        summary = f"Exit {exit_code}, reason: {kill_reason or 'unknown'}. Check previous logs."

    return CrashDiagnosis(
        pod=pod,
        namespace=namespace,
        failure_class=failure_class,
        exit_code=exit_code,
        kill_reason=kill_reason,
        restart_count=status.total_restarts,
        previous_logs=prev_log.lines,
        recent_events=[e for e in events if e.type == EventType.WARNING],
        memory_limit=memory_limit,
        memory_usage=memory_usage,
        summary=summary,
    )


async def diagnose_pending(pod: str, namespace: str = "default") -> PendingDiagnosis:
    """
    Pending pod analysis: scheduling events, node pressure, unbound PVCs, resource requests.
    """
    # Gather in parallel
    events_task  = asyncio.create_task(get_pod_events(pod, namespace))
    nodes_task   = asyncio.create_task(get_node_conditions(pressure_only=True))
    usage_task   = asyncio.create_task(get_pod_resource_usage(pod, namespace))

    rc_pvc, pvc_data, _ = await _kubectl_json("get", "pvc", "-n", namespace)

    events   = await events_task
    nodes    = await nodes_task
    usage    = await usage_task

    scheduling_events = [
        e for e in events
        if e.reason in ("FailedScheduling", "FailedMount", "ProvisioningFailed")
    ]

    pressure_nodes = [n.node for n in nodes]

    unbound_pvcs: list[str] = []
    if rc_pvc == 0:
        for item in pvc_data.get("items", []):
            if item.get("status", {}).get("phase") != "Bound":
                unbound_pvcs.append(item["metadata"]["name"])

    # Build summary
    causes = []
    if scheduling_events:
        causes.append(scheduling_events[0].message[:120])
    if pressure_nodes:
        causes.append(f"Node pressure: {', '.join(pressure_nodes)}")
    if unbound_pvcs:
        causes.append(f"Unbound PVCs: {', '.join(unbound_pvcs)}")

    summary = "; ".join(causes) if causes else "No scheduling events found — check node capacity and affinity rules."

    return PendingDiagnosis(
        pod=pod,
        namespace=namespace,
        scheduling_events=scheduling_events,
        node_pressure_nodes=pressure_nodes,
        unbound_pvcs=unbound_pvcs,
        resource_requests=usage.containers,
        summary=summary,
    )


async def namespace_health_sweep(namespace: str = "default") -> NamespaceHealthReport:
    """
    Full namespace sweep: non-running pods, high-restart pods, warning events,
    node pressure, unbound PVCs. Primary triage entry point.
    """
    pods_task   = asyncio.create_task(get_namespace_pods(namespace))
    events_task = asyncio.create_task(get_namespace_events(namespace, warnings_only=True))
    nodes_task  = asyncio.create_task(get_node_conditions(pressure_only=True))

    rc_pvc, pvc_data, _ = await _kubectl_json("get", "pvc", "-n", namespace)

    pods   = await pods_task
    events = await events_task
    nodes  = await nodes_task

    non_running    = [p for p in pods if p.phase not in ("Running", "Succeeded")]
    high_restart   = [p for p in pods if p.restarts > 3]

    unbound_pvcs: list[str] = []
    if rc_pvc == 0:
        for item in pvc_data.get("items", []):
            if item.get("status", {}).get("phase") != "Bound":
                unbound_pvcs.append(item["metadata"]["name"])

    healthy = (
        len(non_running) == 0
        and len(high_restart) == 0
        and len(events) == 0
        and len(nodes) == 0
    )

    issues = []
    if non_running:
        issues.append(f"{len(non_running)} non-running pod(s): {', '.join(p.name for p in non_running[:3])}")
    if high_restart:
        issues.append(f"{len(high_restart)} high-restart pod(s): {', '.join(p.name for p in high_restart[:3])}")
    if events:
        issues.append(f"{len(events)} warning event(s)")
    if nodes:
        issues.append(f"Node pressure on: {', '.join(n.node for n in nodes)}")

    summary = "Healthy — no issues detected." if healthy else " | ".join(issues)

    return NamespaceHealthReport(
        namespace=namespace,
        total_pods=len(pods),
        running_pods=sum(1 for p in pods if p.phase == "Running"),
        non_running_pods=non_running,
        high_restart_pods=high_restart,
        warning_events=events,
        pressure_nodes=nodes,
        unbound_pvcs=unbound_pvcs,
        healthy=healthy,
        summary=summary,
    )


# ---------------------------------------------------------------------------
# Sync wrapper (for non-async callers)
# ---------------------------------------------------------------------------

def run_sync(coro):
    """Run an async observer function from synchronous code."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, coro)
                return future.result()
        return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Quick CLI smoke-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    async def _smoke():
        pod = sys.argv[1] if len(sys.argv) > 1 else None
        ns  = sys.argv[2] if len(sys.argv) > 2 else "default"

        if pod:
            print("=== Pod status ===")
            s = await get_pod_status(pod, ns)
            print(s.model_dump_json(indent=2))

            print("\n=== Crash diagnosis ===")
            d = await diagnose_crash(pod, ns)
            print(d.model_dump_json(indent=2))
        else:
            print("=== Namespace sweep ===")
            r = await namespace_health_sweep(ns)
            print(r.model_dump_json(indent=2))

    asyncio.run(_smoke())
