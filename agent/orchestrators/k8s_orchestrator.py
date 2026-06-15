"""
K8s domain wiring.

The consume/debounce/bundle/dispatch loop lives in the generic orchestrator
(``agent/orchestrators/generic_orchestrator.py``). This module holds only the two
K8s-specific pieces:

  1. ``_fetch_pods_on_nodes`` — the K8s-API context fetcher (lists pods on the
     affected nodes), injected into the registry as the domain's context_fetcher.
  2. The single k8s ``DomainConfig`` registration (runtime keys stream/group,
     routing/urgency maps, and the wired k8s callables).

Confidence routing (node actions, mode="diagnoser") is unchanged — it now lives
in generic_orchestrator._should_escalate, driven by the escalate_below map below:
    pod_restart          ≥ 0.80 → remediator  (< 0.80 → escalate)
    deployment_scale_down ≥ 0.85 → remediator (< 0.85 → escalate)
    human_escalate        always → escalator
    observe_only          always → remediator (no-op log)

Importing this module registers the k8s domain in agent.registry.REGISTRY.
"""

import structlog
from kubernetes_asyncio import client, config

from agent.registry import DomainConfig, register
from agent.observers.k8s_node_observer import watch_nodes
from agent.skills.agents import node_agent
from agent.skills.diagnosers import node_diagnoser
from agent.skills.remediators import node_remediator

logger = structlog.get_logger()


async def _fetch_pods_on_nodes(node_names: set[str]) -> list[dict]:
    """List pods currently running on the affected nodes."""
    try:
        await config.load_kube_config()
        async with client.ApiClient() as api:
            v1 = client.CoreV1Api(api)
            pod_list = await v1.list_pod_for_all_namespaces()
            pods = []
            for p in pod_list.items:
                if p.spec.node_name not in node_names:
                    continue
                # Extract memory request from first container
                mem_req = "n/a"
                try:
                    mem_req = p.spec.containers[0].resources.requests.get("memory", "n/a")
                except (AttributeError, TypeError, IndexError):
                    pass
                # Derive deployment name: prefer app label, fall back to pod name prefix
                labels = p.metadata.labels or {}
                deployment = (
                    labels.get("app")
                    or labels.get("app.kubernetes.io/name")
                    or "-".join(p.metadata.name.split("-")[:-2])  # strip replicaset+pod hash
                )
                pods.append({
                    "name": p.metadata.name,
                    "namespace": p.metadata.namespace,
                    "deployment": deployment,
                    "phase": p.status.phase or "Unknown",
                    "node": p.spec.node_name,
                    "memory_request": mem_req,
                })
            return pods
    except Exception as exc:
        logger.warning("pod_context_fetch_failed", error=str(exc))
        return []


# Register the k8s domain at import time — the single wiring point for k8s.
# Runtime keys (stream/consumer_group) are preserved exactly; escalate_below and
# urgency_map are the original maps verbatim (do NOT fill in the intentional
# urgency-map gaps, e.g. node_network_unavailable -> p3 fall-through).
register(DomainConfig(
        domain="k8s",
        stream="events:k8s",
        consumer_group="k8s-orchestrator",
        observer=watch_nodes,
        diagnose=node_diagnoser.diagnose,
        remediate=node_remediator.remediate,
        run_incident=node_agent.run_incident,
        context_fetcher=_fetch_pods_on_nodes,
        escalate_below={
            "pod_restart": 0.80,
            "deployment_scale_down": 0.85,
            "human_escalate": 1.1,  # always escalate
            "observe_only": 1.1,    # handled by remediator as no-op
        },
        urgency_map={
            "node_not_ready": "p1_immediate",
            "node_memory_pressure": "p2_within_15m",
            "node_disk_pressure": "p2_within_15m",
            "node_pid_pressure": "p2_within_15m",
        },
))
