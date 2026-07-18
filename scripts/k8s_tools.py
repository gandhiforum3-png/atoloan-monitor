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
]


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------

# Maps tool name → async callable
_TOOL_MAP = {
    "get_pod_status":         obs.get_pod_status,
    "get_pod_logs":           obs.get_pod_logs,
    "get_pod_events":         obs.get_pod_events,
    "get_pod_resource_usage": obs.get_pod_resource_usage,
    "get_namespace_pods":     obs.get_namespace_pods,
    "get_namespace_events":   obs.get_namespace_events,
    "get_node_conditions":    obs.get_node_conditions,
    "get_endpoints":          obs.get_endpoints,
    "diagnose_crash":         obs.diagnose_crash,
    "diagnose_pending":       obs.diagnose_pending,
    "namespace_health_sweep": obs.namespace_health_sweep,
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
