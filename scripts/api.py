"""
api.py — FastAPI HTTP service wrapping the k8s-pod-observer toolkit.

Exposes the L1 runbook and observer functions as REST endpoints so any
service (Atoloan Monitor, alertmanager webhook, CI/CD pipeline) can
trigger a diagnosis or L1 remediation over HTTP.

Endpoints:
  GET  /health                  — liveness probe
  GET  /ready                   — readiness probe (checks kubectl connectivity)
  POST /observe/pod             — get_pod_status for a single pod
  POST /observe/namespace       — namespace_health_sweep
  POST /l1/diagnose             — run the full L1 runbook for a pod
  POST /l1/sweep                — sweep a namespace and diagnose all unhealthy pods

All responses are JSON-serialised Pydantic models.
ANTHROPIC_API_KEY is read from the environment (injected via Kubernetes Secret).
"""

from __future__ import annotations

import asyncio
import os
from typing import Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import pod_observer as obs
from l1_runbook import run_l1_runbook
from models import RunbookResult, NamespaceHealthReport, PodStatus


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(
    title="k8s-pod-observer",
    description="L1 Kubernetes pod observation and remediation service",
    version="1.0.0",
)

API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------

class PodRequest(BaseModel):
    pod:       str
    namespace: str = "default"


class L1DiagnoseRequest(BaseModel):
    pod:        str
    namespace:  str = "default"
    deployment: Optional[str] = None    # enables memory patch, rollback, scale


class L1SweepRequest(BaseModel):
    namespace: str = "default"


class HealthResponse(BaseModel):
    status:  str
    version: str = "1.0.0"


class ReadyResponse(BaseModel):
    status:        str
    kubectl_ok:    bool
    api_key_set:   bool


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------

@app.get("/health", response_model=HealthResponse, tags=["probes"])
async def health() -> HealthResponse:
    """Liveness probe — always returns 200 if the process is running."""
    return HealthResponse(status="ok")


@app.get("/ready", response_model=ReadyResponse, tags=["probes"])
async def ready() -> ReadyResponse:
    """
    Readiness probe — checks that kubectl can reach the cluster and
    that the Anthropic API key is set.
    """
    # Test kubectl connectivity (cheap: just list API server version)
    proc = await asyncio.create_subprocess_exec(
        "kubectl", "version", "--client",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, _ = await proc.communicate()
    kubectl_ok = proc.returncode == 0

    api_key_set = bool(API_KEY)
    status = "ready" if (kubectl_ok and api_key_set) else "degraded"

    return ReadyResponse(
        status=status,
        kubectl_ok=kubectl_ok,
        api_key_set=api_key_set,
    )


# ---------------------------------------------------------------------------
# Observation endpoints
# ---------------------------------------------------------------------------

@app.post("/observe/pod", tags=["observe"])
async def observe_pod(req: PodRequest) -> dict:
    """
    Return current status for a single pod:
    phase, conditions, container states, restart count, failure hint.
    """
    result = await obs.get_pod_status(req.pod, req.namespace)
    return result.model_dump()


@app.post("/observe/namespace", tags=["observe"])
async def observe_namespace(req: L1SweepRequest) -> dict:
    """
    Full namespace health snapshot:
    non-running pods, high-restart pods, warning events, node pressure, unbound PVCs.
    """
    result = await obs.namespace_health_sweep(req.namespace)
    return result.model_dump()


@app.post("/observe/events", tags=["observe"])
async def observe_events(req: PodRequest) -> dict:
    """Return all events for a specific pod, newest first."""
    events = await obs.get_pod_events(req.pod, req.namespace)
    return {"items": [e.model_dump() for e in events]}


@app.post("/observe/logs", tags=["observe"])
async def observe_logs(
    pod: str,
    namespace: str = "default",
    container: Optional[str] = None,
    previous: bool = False,
    tail: int = 100,
) -> dict:
    """Fetch pod logs. Set previous=true for last crashed container output."""
    result = await obs.get_pod_logs(pod, namespace, container, previous, tail)
    return result.model_dump()


# ---------------------------------------------------------------------------
# L1 runbook endpoints
# ---------------------------------------------------------------------------

@app.post("/l1/diagnose", tags=["l1"])
async def l1_diagnose(req: L1DiagnoseRequest) -> dict:
    """
    Run the full L1 troubleshooting runbook for a pod.

    Decision tree: Observe → Classify → Remediate → Verify → Resolve or Escalate.

    Returns RunbookResult with:
      - resolution: "resolved" | "escalated" | "needs_human"
      - severity: P1 | P2 | P3
      - steps: every decision taken
      - remediations: what was tried and whether it worked
      - escalation_packet: structured L2 handoff (if not resolved)
    """
    if not API_KEY:
        raise HTTPException(
            status_code=503,
            detail="ANTHROPIC_API_KEY not set — L2 escalation guidance unavailable. "
                   "Set the secret and restart the pod.",
        )

    result = await run_l1_runbook(
        pod=req.pod,
        namespace=req.namespace,
        deployment=req.deployment,
        api_key=API_KEY,
    )
    return result.model_dump()


@app.post("/l1/sweep", tags=["l1"])
async def l1_sweep(req: L1SweepRequest) -> dict:
    """
    Sweep a namespace: find every non-healthy pod and run the L1 runbook
    against each one in parallel.

    Returns a list of RunbookResult objects — one per unhealthy pod.
    Healthy pods are included with resolution='resolved' for completeness.
    """
    if not API_KEY:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not set.")

    pods = await obs.get_namespace_pods(req.namespace)
    unhealthy = [p for p in pods if p.phase not in ("Running", "Succeeded")]

    if not unhealthy:
        return {
            "namespace": req.namespace,
            "healthy": True,
            "results": [],
            "summary": f"All {len(pods)} pods healthy in namespace '{req.namespace}'.",
        }

    # Run L1 runbook for each unhealthy pod in parallel
    tasks = [
        run_l1_runbook(
            pod=p.name,
            namespace=req.namespace,
            api_key=API_KEY,
        )
        for p in unhealthy
    ]
    results: list[RunbookResult] = await asyncio.gather(*tasks)

    resolved  = sum(1 for r in results if r.resolution.value == "resolved")
    escalated = sum(1 for r in results if r.resolution.value == "escalated")
    human     = sum(1 for r in results if r.resolution.value == "needs_human")

    return {
        "namespace":  req.namespace,
        "healthy":    False,
        "total_pods": len(pods),
        "unhealthy":  len(unhealthy),
        "resolved":   resolved,
        "escalated":  escalated,
        "needs_human": human,
        "results": [r.model_dump() for r in results],
    }
