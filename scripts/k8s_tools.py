"""
k8s_tools.py — Claude API tool definitions for the pod observer functions.

Provides:
  POD_OBSERVER_TOOLS  — list of tool schemas ready to pass to the Claude API
  execute_tool()      — async dispatcher: tool_name + input dict → model result as dict
  tool_result_block() — format a result for a messages API turn

Usage with the Anthropic SDK:
    from k8s_tools import POD_OBSERVER_TOOLS, execute_tool

    response = await client.messages.create(
        model="claude-sonnet-4-6",
        tools=POD_OBSERVER_TOOLS,
        messages=[{"role": "user", "content": "Why is my api pod crashing in prod?"}],
        ...
    )
    # Then pass tool calls through execute_tool() in the agent loop.
    # See orchestrator.py for the full agentic loop.
"""

from __future__ import annotations

import json
from typing import Any

import pod_observer as obs


# ---------------------------------------------------------------------------
# Tool schema definitions
# ---------------------------------------------------------------------------

POD_OBSERVER_TOOLS: list[dict] = [
    {
        "name": "get_pod_status",
        "description": (
            "Get the current status of a Kubernetes pod: phase, ready conditions, "
            "container states, restart counts, and a failure hint. "
            "Always call this first when investigating a pod issue."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pod":       {"type": "string", "description": "Pod name"},
                "namespace": {"type": "string", "description": "Kubernetes namespace", "default": "default"},
            },
            "required": ["pod"],
        },
    },
    {
        "name": "get_pod_logs",
        "description": (
            "Fetch pod logs. Set previous=true to read the last crashed container's output "
            "(essential for CrashLoopBackOff). Returns structured lines."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pod":       {"type": "string"},
                "namespace": {"type": "string", "default": "default"},
                "container": {"type": "string", "description": "Container name (omit for first container)"},
                "previous":  {"type": "boolean", "description": "Read previous (crashed) container logs", "default": False},
                "tail":      {"type": "integer", "description": "Number of log lines to return", "default": 100},
            },
            "required": ["pod"],
        },
    },
    {
        "name": "get_pod_events",
        "description": (
            "Return all Kubernetes events for a specific pod, sorted newest first. "
            "Events surface scheduling failures, probe failures, OOMKills, and volume errors."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pod":       {"type": "string"},
                "namespace": {"type": "string", "default": "default"},
            },
            "required": ["pod"],
        },
    },
    {
        "name": "get_pod_resource_usage",
        "description": (
            "Get live CPU and memory usage for a pod's containers from metrics-server, "
            "alongside the configured requests and limits. "
            "Use when investigating OOMKilled or CPU throttling."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pod":       {"type": "string"},
                "namespace": {"type": "string", "default": "default"},
            },
            "required": ["pod"],
        },
    },
    {
        "name": "get_namespace_pods",
        "description": (
            "List all pods in a namespace with phase, ready state, restart count, and node. "
            "Use for a quick overview before drilling into a specific pod."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace":      {"type": "string", "default": "default"},
                "all_namespaces": {"type": "boolean", "default": False,
                                   "description": "Set true to list pods across all namespaces"},
            },
            "required": [],
        },
    },
    {
        "name": "get_namespace_events",
        "description": (
            "Fetch recent events from a namespace, newest first. "
            "Set warnings_only=true (default) to surface only Warning events — "
            "the fastest way to spot what is actively failing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace":    {"type": "string", "default": "default"},
                "warnings_only": {"type": "boolean", "default": True},
                "limit":        {"type": "integer", "default": 30},
            },
            "required": [],
        },
    },
    {
        "name": "get_node_conditions",
        "description": (
            "Return node health conditions. By default returns only nodes with pressure "
            "(MemoryPressure, DiskPressure, PIDPressure) or NotReady status. "
            "Use when pods are Pending or being Evicted."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pressure_only": {"type": "boolean", "default": True,
                                  "description": "Only return nodes with active pressure conditions"},
            },
            "required": [],
        },
    },
    {
        "name": "get_endpoints",
        "description": (
            "Check whether a Kubernetes Service has ready endpoint addresses. "
            "An empty ready_addresses list means no pods are receiving traffic — "
            "readiness probe is failing or no pods match the selector."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "service":   {"type": "string"},
                "namespace": {"type": "string", "default": "default"},
            },
            "required": ["service"],
        },
    },
    {
        "name": "diagnose_crash",
        "description": (
            "Composite crash diagnosis: runs get_pod_status, get_pod_events, "
            "get_pod_logs(previous=true), and get_pod_resource_usage in parallel, "
            "then returns a structured CrashDiagnosis with exit_code, kill_reason, "
            "previous_logs, and a plain-English summary. "
            "Use when a pod is in CrashLoopBackOff or shows OOMKilled in its last state."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pod":       {"type": "string"},
                "namespace": {"type": "string", "default": "default"},
            },
            "required": ["pod"],
        },
    },
    {
        "name": "diagnose_pending",
        "description": (
            "Composite pending diagnosis: scheduling events, node pressure, unbound PVCs, "
            "and resource requests. Use when a pod is stuck in Pending."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pod":       {"type": "string"},
                "namespace": {"type": "string", "default": "default"},
            },
            "required": ["pod"],
        },
    },
    {
        "name": "namespace_health_sweep",
        "description": (
            "Full namespace health sweep: non-running pods, high-restart pods, "
            "warning events, node pressure, and unbound PVCs — all in one call. "
            "Use as the starting point for 'what is wrong in this namespace?'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "default": "default"},
            },
            "required": [],
        },
    },
    {
        "name": "resolve_owning_deployment",
        "description": (
            "Walk pod -> ReplicaSet -> Deployment via ownerReferences. Returns the "
            "owning Deployment name, or null if the pod isn't Deployment-managed. "
            "Call this early — most remediation actions need the deployment name."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pod":       {"type": "string"},
                "namespace": {"type": "string", "default": "default"},
            },
            "required": ["pod"],
        },
    },
    {
        "name": "diagnose_container_config_error",
        "description": (
            "Audit a pod spec's env/envFrom/volumes for ConfigMap and Secret references "
            "and check each referenced object actually exists. Use when a container is "
            "waiting with reason CreateContainerConfigError. If all references exist it's "
            "usually a startup race (safe to restart); if any are missing, name them."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pod":       {"type": "string"},
                "namespace": {"type": "string", "default": "default"},
            },
            "required": ["pod"],
        },
    },
    {
        "name": "diagnose_init_container_crash",
        "description": (
            "Init-container equivalent of diagnose_crash(): pinpoints which init "
            "container is failing, its exit code/reason, and that container's "
            "previous-crash logs. Use when a pod is stuck Pending/Init because an "
            "init container is crash-looping."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pod":       {"type": "string"},
                "namespace": {"type": "string", "default": "default"},
            },
            "required": ["pod"],
        },
    },
    {
        "name": "diagnose_stuck_container_creating",
        "description": (
            "How long a pod has been in ContainerCreating, and whether events point "
            "to a specific cause (volume attach delay, CNI/sandbox failure) vs. just "
            "a slow-but-progressing image pull. Use when a pod is stuck ContainerCreating."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pod":       {"type": "string"},
                "namespace": {"type": "string", "default": "default"},
            },
            "required": ["pod"],
        },
    },
    {
        "name": "check_pod_node_pressure",
        "description": (
            "Check whether the node a Running pod is scheduled on has active "
            "MemoryPressure/DiskPressure/PIDPressure/NotReady conditions — catches "
            "pressure building up before an eviction happens. Meaningful for pods "
            "that are already Running, unlike get_node_conditions used during Pending."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pod":       {"type": "string"},
                "namespace": {"type": "string", "default": "default"},
            },
            "required": ["pod"],
        },
    },
    {
        "name": "diagnose_endpoint_mismatch",
        "description": (
            "Check whether a pod's labels actually satisfy a Service's selector. "
            "Catches the case where a pod is perfectly healthy but a label typo "
            "means no Service ever routes traffic to it. Omit `service` to "
            "auto-discover a candidate Service in the namespace."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pod":       {"type": "string"},
                "namespace": {"type": "string", "default": "default"},
                "service":   {"type": "string", "description": "Service name (omit to auto-discover)"},
            },
            "required": ["pod"],
        },
    },
    {
        "name": "get_deployment_conditions",
        "description": (
            "Read a Deployment's .status.conditions. Flags ProgressDeadlineExceeded — "
            "a stuck rollout where the new ReplicaSet never became healthy. "
            "This is a Deployment-level condition, not visible from any pod-level check."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "deployment": {"type": "string"},
                "namespace":  {"type": "string", "default": "default"},
            },
            "required": ["deployment"],
        },
    },
    {
        "name": "get_resourcequota_status",
        "description": (
            "Read ResourceQuota objects in a namespace and flag any dimension at or "
            "over its hard limit. Use when a Deployment scale-up or pod create is "
            "silently failing at the namespace quota boundary."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "default": "default"},
            },
            "required": [],
        },
    },
    {
        "name": "get_pdb_status",
        "description": (
            "Read PodDisruptionBudget status for a namespace. disruptions_allowed=0 "
            "means a voluntary eviction/drain will be blocked. Omit `name` to get the "
            "most restrictive PDB in the namespace."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "default": "default"},
                "name":      {"type": "string", "description": "PDB name (omit for most-restrictive in namespace)"},
            },
            "required": [],
        },
    },
    {
        "name": "get_hpa_status",
        "description": (
            "Read a HorizontalPodAutoscaler's .status.conditions. Flags AbleToScale=False "
            "or ScalingActive=False — usually metrics-server unavailable or misconfigured metrics."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name":      {"type": "string", "description": "HPA name"},
                "namespace": {"type": "string", "default": "default"},
            },
            "required": ["name"],
        },
    },
]


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------

# Maps tool name → async callable
_TOOL_MAP = {
    "get_pod_status":                    obs.get_pod_status,
    "get_pod_logs":                      obs.get_pod_logs,
    "get_pod_events":                    obs.get_pod_events,
    "get_pod_resource_usage":            obs.get_pod_resource_usage,
    "get_namespace_pods":                obs.get_namespace_pods,
    "get_namespace_events":              obs.get_namespace_events,
    "get_node_conditions":               obs.get_node_conditions,
    "get_endpoints":                     obs.get_endpoints,
    "diagnose_crash":                    obs.diagnose_crash,
    "diagnose_pending":                  obs.diagnose_pending,
    "namespace_health_sweep":            obs.namespace_health_sweep,
    "resolve_owning_deployment":         obs.resolve_owning_deployment,
    "diagnose_container_config_error":   obs.diagnose_container_config_error,
    "diagnose_init_container_crash":     obs.diagnose_init_container_crash,
    "diagnose_stuck_container_creating": obs.diagnose_stuck_container_creating,
    "check_pod_node_pressure":           obs.check_pod_node_pressure,
    "diagnose_endpoint_mismatch":        obs.diagnose_endpoint_mismatch,
    "get_deployment_conditions":         obs.get_deployment_conditions,
    "get_resourcequota_status":          obs.get_resourcequota_status,
    "get_pdb_status":                    obs.get_pdb_status,
    "get_hpa_status":                    obs.get_hpa_status,
}


async def execute_tool(tool_name: str, tool_input: dict[str, Any]) -> dict:
    """
    Dispatch a Claude tool_use block to the correct observer function.
    Returns a JSON-serialisable dict (the Pydantic model's .model_dump()).

    Raises KeyError for unknown tool names.
    """
    fn = _TOOL_MAP[tool_name]
    result = await fn(**tool_input)

    # Pydantic models → dict; lists of models → list of dicts
    if hasattr(result, "model_dump"):
        return result.model_dump()
    if isinstance(result, list):
        return {
            "items": [
                r.model_dump() if hasattr(r, "model_dump") else r
                for r in result
            ]
        }
    return {"result": result}


def tool_result_block(tool_use_id: str, result: dict) -> dict:
    """
    Format a tool result for the Claude messages API.

    Usage:
        block = tool_result_block(tool_use_id=block.id, result=await execute_tool(...))
        messages.append({"role": "user", "content": [block]})
    """
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": json.dumps(result, default=str),
    }
