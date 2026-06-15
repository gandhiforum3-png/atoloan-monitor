"""
Node condition diagnoser.

Calls Claude Sonnet with a signal bundle of K8s node events and returns
a structured DiagnosisResult. Uses prompt caching for the static
infrastructure topology so repeated calls in a session hit the cache.
"""

import structlog
from langchain_anthropic import ChatAnthropic

from agent.shared.diagnoser_base import diagnose_with_claude
from agent.shared.models import DiagnosisResult, SignalBundle

logger = structlog.get_logger()

# This domain's known action vocabulary. After D-01 opened DiagnosisResult.action_type
# to an open str, the shared model's JSON schema no longer carries an enum. We re-inject
# THIS domain's enum into the tool input_schema below (belt-and-suspenders per research
# Open Question 1) so the Claude API call for node diagnosis stays constrained to the 4
# known k8s actions — without re-closing the shared model for every other domain. The
# boundary (threshold + safety_check) remains the real defense.
_K8S_ACTION_TYPES = ["pod_restart", "deployment_scale_down", "human_escalate", "observe_only"]


def _tool_input_schema() -> dict:
    """DiagnosisResult schema with this domain's action_type enum re-injected."""
    schema = DiagnosisResult.model_json_schema()
    schema["properties"]["action_type"]["enum"] = list(_K8S_ACTION_TYPES)
    return schema


# Cached across all calls in a session — only re-tokenized when the system prompt changes.
_SYSTEM_PROMPT = """\
You are an expert SRE diagnosing Kubernetes node infrastructure incidents for Atoloan.

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
                               human_escalate with estimated_blast_radius >= "cluster".

## Confidence Calibration
- 0.9–1.0: Multiple corroborating signals, complete causal chain, no alternative explanation
- 0.7–0.8: Strong evidence, one alternative cannot be ruled out
- 0.5–0.6: Circumstantial, temporal correlation without direct causation
- 0.4–0.5: Speculative, key signals missing
- < 0.4:   Insufficient data to diagnose — set requires_human_review=true

## Action Types
- observe_only:        Insufficient confidence; monitor for more signals
- human_escalate:      Confidence below action threshold, or risk too high to automate
- pod_restart:         Restart a specific misbehaving pod to free resources (confidence ≥ 0.80)
- deployment_scale_down: Temporarily reduce replicas to free node memory (confidence ≥ 0.85)

Important: If you are uncertain, return a lower confidence score and set requires_human_review=true.
It is better to escalate than to act on a speculative diagnosis.
"""


def _format_bundle(bundle: SignalBundle, pods_on_node: list[dict]) -> str:
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

    if pods_on_node:
        lines += ["", "## Pods currently on affected node(s)"]
        lines.append("  Format: namespace/deployment  (pod_name)  phase  memory_request")
        for pod in pods_on_node:
            lines.append(
                f"  - {pod['namespace']}/{pod.get('deployment', '?')}  "
                f"({pod['name']})  phase={pod['phase']}"
                f"  memory_request={pod.get('memory_request', 'n/a')}"
            )
        lines.append("")
        lines.append(
            "  NOTE: Use 'namespace/deployment' format in affected_components "
            "(e.g. 'atoloan-backend-prod/atoloan-api'), not the full pod name."
        )
    else:
        lines += ["", "## Pods on affected node(s)", "  (none found or context unavailable)"]

    return "\n".join(lines)


async def diagnose(
    client: ChatAnthropic,
    bundle: SignalBundle,
    pods_on_node: list[dict],
) -> DiagnosisResult:
    """
    Call Claude Sonnet and return a structured DiagnosisResult.

    Args:
        client:        ChatAnthropic client.
        bundle:        Signal bundle from the orchestrator.
        pods_on_node:  Pod metadata for pods running on the affected nodes.
    """
    user_text = _format_bundle(bundle, pods_on_node)
    logger.info(
        "diagnoser_start",
        signal_count=len(bundle.signals),
        pod_context_count=len(pods_on_node),
    )

    return await diagnose_with_claude(
        client,
        _SYSTEM_PROMPT,
        user_text,
        tool_description="Submit the completed RCA diagnosis for these node conditions",
        input_schema=_tool_input_schema(),  # re-inject per-domain enum (D-01 backstop)
    )
