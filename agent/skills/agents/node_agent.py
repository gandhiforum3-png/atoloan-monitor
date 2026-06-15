"""
K8s node incident agent — agentic-loop alternative to node_diagnoser + node_remediator.

Instead of one Claude call returning a structured diagnosis that Python then
acts on, Claude drives a tool-use loop: it can inspect live cluster state,
take a remediation action, verify the result, and escalate — all within one
incident.

Safety boundary is unchanged from node_remediator:
  - Every mutating tool re-runs safety_check() before touching the cluster.
  - Confidence thresholds (THRESHOLDS) and pre-flight checks (cooldown, PDB,
    replica floor) are enforced inside the tool, not by Claude.
  - learning_mode=True (Phase 1) blocks all mutating tools the same way
    node_remediator does — "would execute" is logged but nothing is patched.

Selected via k8s_orchestrator's mode="agent" (vs. the default mode="diagnoser",
which uses node_diagnoser + node_remediator). Both paths can be run and compared.
"""

import structlog
from anthropic import AsyncAnthropic
from kubernetes_asyncio import client
from kubernetes_asyncio.client import ApiException
from redis.asyncio import Redis

from agent.shared.agent_loop import run_tool_loop
from agent.shared.models import DiagnosisResult, SignalBundle
from agent.shared.safety import SafetyViolation, safety_check
from agent.skills.remediators import human_escalator
from agent.shared.remediation import (
    THRESHOLDS,
    _log_action,
    _meets_threshold,
)
from agent.skills.remediators.node_remediator import (
    _execute_pod_restart,
    _execute_scale_down,
    _load_k8s_config,
    _preflight_pod_restart,
    _preflight_scale_down,
)

logger = structlog.get_logger()

MAX_ITERATIONS = 6

_SYSTEM_PROMPT = """\
You are an expert SRE agent diagnosing and remediating Kubernetes node infrastructure
incidents for Atoloan. Unlike a one-shot diagnosis, you have tools to investigate live
cluster state, take a remediation action, verify the result, and escalate to a human —
all within this single incident.

## Atoloan Infrastructure Topology
- Cloud: AWS us-east-2, k3s Kubernetes cluster
- Nodes:
    - atoloan-k8s-prod-server (t3.small): control plane + ingress-nginx + cert-manager
    - atoloan-k8s-prod-agent  (t3.small): workload node — all application pods scheduled here
- Namespaces and workloads:
    - atoloan-frontend-prod: React/nginx, 1 replica (100m CPU / 128Mi RAM request)
    - atoloan-backend-prod:  FastAPI/uvicorn, 2 replicas (300m CPU / 512Mi RAM request each)
    - ingress-nginx, cert-manager, external-secrets: system pods on server node
- Database: AWS RDS PostgreSQL 16 (db.t4g.micro) — NOT on K8s nodes, in private subnet
- Storage: 20 GB gp3 + 2 GB swap per node

## Node Conditions Reference
- MemoryPressure=True  → Node low on memory; kubelet will evict best-effort pods first
- DiskPressure=True    → Node low on disk; new pods cannot be scheduled
- PIDPressure=True     → Node low on process IDs; may fail to start new containers
- Ready=False/Unknown  → Node unhealthy or API server cannot reach it
- NetworkUnavailable=True   → Node network is not correctly configured; pods on this node
                               may be unreachable. No pod-level fix exists — escalate.
- node_deleted (event_type) → The node object disappeared from the K8s API entirely
                               (terminated, crashed, or deregistered). This is NOT a
                               pod_restart/scale_down situation — drain/cordon/replace are
                               forbidden operations, so the only correct action is
                               escalate_human with estimated_blast_radius >= "cluster".

## Your Tools
- get_node_status(node_name)        — re-read current node conditions from the API
- get_pods_on_node(node_name)       — list pods scheduled on a node
- restart_deployment(...)           — rolling restart (executes only if confidence >= 0.80)
- scale_down_deployment(...)        — reduce replicas by 1, min 1 (executes only if confidence >= 0.85)
- verify_pod_healthy(...)           — check pod/replica health after an action
- escalate_human(...)               — hand off to a human operator
- finish_incident(outcome, summary) — call this LAST, always, to close out the incident

## Safety
- Destructive operations (delete pod, drain/cordon node, delete deployment, etc.) are not
  available to you — they are hard-blocked at the system level and cannot be requested.
- restart_deployment and scale_down_deployment enforce their own confidence thresholds,
  cooldowns, and PodDisruptionBudget checks — they will refuse and report back if a
  pre-flight check fails. Adjust your plan accordingly (e.g. escalate instead).
- In learning mode, mutating actions are logged as "would execute" but never applied —
  you can still call them to record your intended action; verify_pod_healthy will then
  reflect the unmodified state.

## Confidence Calibration
- 0.9-1.0: Multiple corroborating signals, complete causal chain, no alternative explanation
- 0.7-0.8: Strong evidence, one alternative cannot be ruled out
- 0.5-0.6: Circumstantial, temporal correlation without direct causation
- < 0.5:   Insufficient data — escalate rather than act

## Workflow
1. Investigate: use get_node_status / get_pods_on_node to confirm and scope the problem.
2. Act (if warranted): call restart_deployment or scale_down_deployment with your
   confidence and reasoning. If confidence is too low, escalate instead.
3. Verify: after any action, call verify_pod_healthy to confirm it helped.
4. Finish: always end by calling finish_incident with an outcome and a short summary.

You have a limited number of tool-call rounds — be efficient. If you have not resolved
or escalated within your budget, the system will escalate automatically.
"""

TOOLS = [
    {
        "name": "get_node_status",
        "description": (
            "Fetch the live condition list (MemoryPressure, DiskPressure, PIDPressure, "
            "Ready, etc.) for a K8s node directly from the API server."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "node_name": {"type": "string", "description": "Name of the node"},
            },
            "required": ["node_name"],
        },
    },
    {
        "name": "get_pods_on_node",
        "description": (
            "List all pods currently scheduled on a node, with namespace, deployment, "
            "phase, and memory request."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "node_name": {"type": "string", "description": "Name of the node"},
            },
            "required": ["node_name"],
        },
    },
    {
        "name": "restart_deployment",
        "description": (
            "Trigger a rolling restart of a deployment (patches the pod template "
            "annotation — does NOT delete pods). Subject to a 300s cooldown, "
            "PodDisruptionBudget, and available-replica pre-flight checks. Only executes "
            "if confidence >= 0.80; below that, or in learning mode, the action is logged "
            "but not performed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string"},
                "deployment": {"type": "string"},
                "confidence": {
                    "type": "number", "minimum": 0, "maximum": 1,
                    "description": "Your confidence that restarting this deployment will resolve the issue",
                },
                "reasoning": {"type": "string", "description": "Why this action should fix the problem"},
            },
            "required": ["namespace", "deployment", "confidence", "reasoning"],
        },
    },
    {
        "name": "scale_down_deployment",
        "description": (
            "Reduce a deployment's replica count by 1 (never below 1) to free node "
            "resources. Subject to PodDisruptionBudget and minimum-replica pre-flight "
            "checks. Only executes if confidence >= 0.85; below that, or in learning "
            "mode, the action is logged but not performed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string"},
                "deployment": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "reasoning": {"type": "string"},
            },
            "required": ["namespace", "deployment", "confidence", "reasoning"],
        },
    },
    {
        "name": "verify_pod_healthy",
        "description": (
            "Re-check a deployment's pods after taking an action — returns ready/desired "
            "replica counts and pod phases. Use this to confirm a remediation worked "
            "before finishing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string"},
                "deployment": {"type": "string"},
            },
            "required": ["namespace", "deployment"],
        },
    },
    {
        "name": "escalate_human",
        "description": (
            "Escalate this incident to a human operator instead of acting automatically. "
            "Use when confidence is low, risk is too high, or remediation did not resolve "
            "the issue."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "root_cause": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "confidence_reasoning": {"type": "string"},
                "recommended_action": {"type": "string"},
                "affected_components": {"type": "array", "items": {"type": "string"}},
                "contributing_factors": {"type": "array", "items": {"type": "string"}},
                "estimated_blast_radius": {
                    "type": "string", "enum": ["service", "cluster", "datacenter"],
                },
                "why_escalated": {"type": "string"},
            },
            "required": [
                "root_cause", "confidence", "confidence_reasoning",
                "recommended_action", "estimated_blast_radius", "why_escalated",
            ],
        },
    },
    {
        "name": "finish_incident",
        "description": (
            "Call this LAST, always, to close out the incident — whether it was "
            "resolved, escalated, or required no action."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "outcome": {
                    "type": "string",
                    "enum": ["resolved", "escalated", "no_action_needed", "unresolved"],
                },
                "summary": {"type": "string", "description": "1-3 sentence summary of what was found and done"},
            },
            "required": ["outcome", "summary"],
        },
    },
]


def _format_bundle(bundle: SignalBundle) -> str:
    lines = [
        f"Signal window: {bundle.window_seconds}s starting {bundle.started_at.isoformat()}",
        f"Signal count: {len(bundle.signals)}",
        "",
        "## Signals",
    ]
    for s in bundle.signals:
        p = s.raw_payload
        lines.append(
            f"  - node={s.resource_id}  condition={p.get('condition_type')}={p.get('status')}"
            f"  reason={p.get('reason')}  last_transition={p.get('last_transition')}"
        )
        if p.get("message"):
            lines.append(f"      message: {p['message'][:200]}")

    lines += [
        "",
        "Investigate using the available tools, take action if appropriate, verify the "
        "result, then call finish_incident.",
    ]
    return "\n".join(lines)


async def _tool_get_node_status(ctx: dict, node_name: str) -> dict:
    try:
        node = await ctx["core_v1"].read_node_status(node_name)
    except ApiException as exc:
        return {"error": f"could not read node '{node_name}': {exc.reason}"}

    conditions = []
    for cond in node.status.conditions or []:
        conditions.append({
            "type": cond.type,
            "status": cond.status,
            "reason": cond.reason or "",
            "message": cond.message or "",
            "last_transition": cond.last_transition_time.isoformat() if cond.last_transition_time else None,
        })
    return {"node": node_name, "conditions": conditions}


async def _tool_get_pods_on_node(ctx: dict, node_name: str) -> dict:
    pod_list = await ctx["core_v1"].list_pod_for_all_namespaces()
    pods = []
    for p in pod_list.items:
        if p.spec.node_name != node_name:
            continue
        mem_req = "n/a"
        try:
            mem_req = p.spec.containers[0].resources.requests.get("memory", "n/a")
        except (AttributeError, TypeError, IndexError):
            pass
        labels = p.metadata.labels or {}
        deployment = (
            labels.get("app")
            or labels.get("app.kubernetes.io/name")
            or "-".join(p.metadata.name.split("-")[:-2])
        )
        pods.append({
            "name": p.metadata.name,
            "namespace": p.metadata.namespace,
            "deployment": deployment,
            "phase": p.status.phase or "Unknown",
            "memory_request": mem_req,
        })
    return {"node": node_name, "pods": pods}


async def _tool_restart_deployment(
    ctx: dict, namespace: str, deployment: str, confidence: float, reasoning: str
) -> dict:
    action = "pod_restart"
    target = f"{namespace}/{deployment}"
    redis, incident_id, learning_mode = ctx["redis"], ctx["incident_id"], ctx["learning_mode"]

    try:
        safety_check(action, target)
    except SafetyViolation as exc:
        await _log_action(redis, incident_id, "k8s", action, "safety_blocked", confidence, str(exc))
        return {"status": "safety_blocked", "detail": str(exc)}

    if not _meets_threshold("k8s", action, confidence):
        detail = f"confidence {confidence:.2f} < threshold {THRESHOLDS['k8s'][action]}"
        await _log_action(redis, incident_id, "k8s", action, "threshold_not_met", confidence, detail)
        return {"status": "threshold_not_met", "detail": detail}

    if learning_mode:
        detail = (
            f"WOULD EXECUTE pod_restart on {target}. Reasoning: {reasoning}. "
            f"Confidence: {confidence:.2f}. Blocked by learning_mode=True."
        )
        await _log_action(redis, incident_id, "k8s", action, "learning_mode_blocked", confidence, detail)
        return {"status": "learning_mode_blocked", "detail": detail}

    preflight = await _preflight_pod_restart(ctx["apps_v1"], ctx["policy_v1"], namespace, deployment)
    if not preflight.ok:
        detail = f"pre-flight failed for {action} on {target}: {preflight.reason}"
        await _log_action(redis, incident_id, "k8s", action, "preflight_failed", confidence, detail)
        return {"status": "preflight_failed", "detail": detail}

    try:
        await _execute_pod_restart(ctx["apps_v1"], namespace, deployment)
    except ApiException as exc:
        detail = f"K8s API error during {action} on {target}: {exc.reason}"
        await _log_action(redis, incident_id, "k8s", action, "execution_failed", confidence, detail)
        return {"status": "execution_failed", "detail": detail}

    detail = f"Rolling restart triggered for {target}. Reasoning: {reasoning}"
    await _log_action(redis, incident_id, "k8s", action, "executed", confidence, detail)
    return {"status": "executed", "detail": detail}


async def _tool_scale_down_deployment(
    ctx: dict, namespace: str, deployment: str, confidence: float, reasoning: str
) -> dict:
    action = "deployment_scale_down"
    target = f"{namespace}/{deployment}"
    redis, incident_id, learning_mode = ctx["redis"], ctx["incident_id"], ctx["learning_mode"]

    try:
        safety_check(action, target)
    except SafetyViolation as exc:
        await _log_action(redis, incident_id, "k8s", action, "safety_blocked", confidence, str(exc))
        return {"status": "safety_blocked", "detail": str(exc)}

    if not _meets_threshold("k8s", action, confidence):
        detail = f"confidence {confidence:.2f} < threshold {THRESHOLDS['k8s'][action]}"
        await _log_action(redis, incident_id, "k8s", action, "threshold_not_met", confidence, detail)
        return {"status": "threshold_not_met", "detail": detail}

    if learning_mode:
        detail = (
            f"WOULD EXECUTE deployment_scale_down on {target}. Reasoning: {reasoning}. "
            f"Confidence: {confidence:.2f}. Blocked by learning_mode=True."
        )
        await _log_action(redis, incident_id, "k8s", action, "learning_mode_blocked", confidence, detail)
        return {"status": "learning_mode_blocked", "detail": detail}

    preflight, current_replicas = await _preflight_scale_down(ctx["apps_v1"], ctx["policy_v1"], namespace, deployment)
    if not preflight.ok:
        detail = f"pre-flight failed for {action} on {target}: {preflight.reason}"
        await _log_action(redis, incident_id, "k8s", action, "preflight_failed", confidence, detail)
        return {"status": "preflight_failed", "detail": detail}

    try:
        new_replicas = await _execute_scale_down(ctx["apps_v1"], namespace, deployment, current_replicas)
    except ApiException as exc:
        detail = f"K8s API error during {action} on {target}: {exc.reason}"
        await _log_action(redis, incident_id, "k8s", action, "execution_failed", confidence, detail)
        return {"status": "execution_failed", "detail": detail}

    detail = f"Scaled {target} from {current_replicas} to {new_replicas} replicas. Reasoning: {reasoning}"
    await _log_action(redis, incident_id, "k8s", action, "executed", confidence, detail)
    return {"status": "executed", "detail": detail, "replicas": new_replicas}


async def _tool_verify_pod_healthy(ctx: dict, namespace: str, deployment: str) -> dict:
    apps_v1, core_v1 = ctx["apps_v1"], ctx["core_v1"]
    try:
        dep = await apps_v1.read_namespaced_deployment(deployment, namespace)
    except ApiException as exc:
        return {"error": f"deployment '{namespace}/{deployment}' not found: {exc.reason}"}

    selector = (dep.spec.selector.match_labels or {}) if dep.spec.selector else {}
    selector_str = ",".join(f"{k}={v}" for k, v in selector.items())
    pod_list = await core_v1.list_namespaced_pod(namespace, label_selector=selector_str)

    pods = []
    for p in pod_list.items:
        restarts = sum(c.restart_count for c in (p.status.container_statuses or []))
        pods.append({"name": p.metadata.name, "phase": p.status.phase, "restarts": restarts})

    desired = dep.spec.replicas or 0
    ready = dep.status.ready_replicas or 0
    return {
        "namespace": namespace,
        "deployment": deployment,
        "desired_replicas": desired,
        "ready_replicas": ready,
        "healthy": ready >= desired and all(p["phase"] == "Running" for p in pods),
        "pods": pods,
    }


async def _tool_escalate_human(
    ctx: dict,
    *,
    root_cause: str,
    confidence: float,
    confidence_reasoning: str,
    recommended_action: str,
    estimated_blast_radius: str,
    why_escalated: str,
    affected_components: list[str] | None = None,
    contributing_factors: list[str] | None = None,
) -> dict:
    diagnosis = DiagnosisResult(
        root_cause=root_cause,
        affected_components=affected_components or [],
        contributing_factors=contributing_factors or [],
        confidence=confidence,
        confidence_reasoning=confidence_reasoning,
        recommended_action=recommended_action,
        action_type="human_escalate",
        requires_human_review=True,
        estimated_blast_radius=estimated_blast_radius,
    )
    escalation_id = await human_escalator.escalate(
        ctx["redis"], domain="k8s", diagnosis=diagnosis, signals=ctx["signals"], why=why_escalated,
    )
    return {"status": "escalated", "escalation_id": escalation_id}


async def _dispatch_tool(name: str, tool_input: dict, ctx: dict) -> dict:
    if name == "get_node_status":
        return await _tool_get_node_status(ctx, tool_input["node_name"])
    if name == "get_pods_on_node":
        return await _tool_get_pods_on_node(ctx, tool_input["node_name"])
    if name == "restart_deployment":
        return await _tool_restart_deployment(ctx, **tool_input)
    if name == "scale_down_deployment":
        return await _tool_scale_down_deployment(ctx, **tool_input)
    if name == "verify_pod_healthy":
        return await _tool_verify_pod_healthy(ctx, tool_input["namespace"], tool_input["deployment"])
    if name == "escalate_human":
        return await _tool_escalate_human(ctx, **tool_input)
    return {"error": f"unknown tool '{name}'"}


async def run_incident(
    anthropic_client: AsyncAnthropic,
    redis: Redis,
    bundle: SignalBundle,
    incident_id: str,
    *,
    learning_mode: bool = True,
) -> str:
    """
    Run the agentic tool-use loop for one incident.

    Performs the K8s-specific context setup (API clients), then delegates the
    domain-agnostic iterate / dispatch / finish / auto-escalate loop to
    run_tool_loop. Returns the outcome string from finish_incident (or a
    fallback status if the loop did not converge within MAX_ITERATIONS).
    """
    await _load_k8s_config()
    async with client.ApiClient() as api:
        ctx = {
            "redis": redis,
            "incident_id": incident_id,
            "learning_mode": learning_mode,
            "signals": bundle.signals,
            "core_v1": client.CoreV1Api(api),
            "apps_v1": client.AppsV1Api(api),
            "policy_v1": client.PolicyV1Api(api),
        }

        return await run_tool_loop(
            anthropic_client,
            redis,
            bundle,
            incident_id,
            system_prompt=_SYSTEM_PROMPT,
            tools=TOOLS,
            dispatch_tool=_dispatch_tool,
            format_bundle=_format_bundle,
            ctx=ctx,
            domain="k8s",
            learning_mode=learning_mode,
            max_iterations=MAX_ITERATIONS,
        )
