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
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel

import pod_observer as obs
import watcher
from l1_runbook import run_l1_runbook
from models import RunbookResult, NamespaceHealthReport, PodStatus


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
WATCHER_ENABLED = os.environ.get("WATCHER_ENABLED", "true").lower() == "true"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    if WATCHER_ENABLED:
        watcher.start()
    yield
    watcher.stop()


app = FastAPI(
    title="k8s-pod-observer",
    description="L1 Kubernetes pod observation and remediation service",
    version="1.0.0",
    lifespan=_lifespan,
)

STATIC_DIR = Path(__file__).parent / "static"


# ---------------------------------------------------------------------------
# Dashboard UI
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse, tags=["ui"])
async def dashboard() -> HTMLResponse:
    """Serves the built-in incident timeline / diagnose / logs dashboard."""
    return HTMLResponse((STATIC_DIR / "dashboard.html").read_text())


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
    api_key:    Optional[str] = None    # overrides ANTHROPIC_API_KEY for this call


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
# Watcher endpoints
# ---------------------------------------------------------------------------

@app.get("/watch/status", tags=["watch"])
async def watch_status() -> dict:
    """
    Watcher loop status: whether the Kubernetes watch stream is connected,
    how many pod events it has seen, and how many diagnoses it has triggered.
    """
    return watcher.status()


@app.get("/watch/live", tags=["watch"])
async def watch_live() -> dict:
    """
    Pods that are unhealthy RIGHT NOW, cluster-wide — derived from current
    cluster state, not the history log. A pod that was escalated an hour ago
    but has since recovered, been rescheduled, or deleted will not appear
    here even though it's still in /watch/history.
    """
    pods = await obs.get_namespace_pods(all_namespaces=True)
    unhealthy = []
    for p in pods:
        ready_count, _, total_count = p.ready.partition("/")
        ready_count = int(ready_count) if ready_count.isdigit() else 0
        total_count = int(total_count) if total_count.isdigit() else 0
        # Succeeded (completed Jobs) are healthy by definition even though
        # their containers show 0/N ready after exiting — don't flag those.
        # Historical restart counts don't matter here either: a pod that's
        # Running and fully ready right now is not a live issue, no matter
        # how many times it crashed in the past (that's what /watch/history
        # is for). Only current phase/readiness counts as "live".
        is_unhealthy = (
            p.phase == "Failed"
            or p.phase in ("Pending", "Unknown")
            or (p.phase == "Running" and ready_count < total_count)
        )
        if is_unhealthy:
            unhealthy.append(p.model_dump())
    return {"count": len(unhealthy), "items": unhealthy}


@app.get("/watch/history", tags=["watch"])
async def watch_history(limit: int = 50) -> dict:
    """
    Recent diagnoses the watcher triggered on its own (newest first), without
    waiting for a manual /l1/diagnose call. Diagnosis-only — the watcher never
    passes `deployment`, so nothing here was auto-remediated.
    """
    items = list(watcher.history)[:limit]
    return {"count": len(items), "items": items}


@app.get("/watch/needs-attention", tags=["watch"])
async def watch_needs_attention() -> dict:
    """
    Things L1 couldn't fix on its own and a human should look at — i.e. the
    most recent history entry per pod where resolution was "escalated" or
    "needs_human", filtered down to ones that are still actually a problem
    right now (the pod still exists and hasn't since become healthy on its
    own). A pod that recovered or was deleted after being escalated won't
    show up here even though the escalation is still in /watch/history.
    """
    latest_by_pod: dict[tuple[str, str], dict] = {}
    for item in watcher.history:
        key = (item["pod"], item["namespace"])
        if key not in latest_by_pod:  # history is newest-first
            latest_by_pod[key] = item

    candidates = [
        item for item in latest_by_pod.values()
        if item["resolution"] in ("escalated", "needs_human")
    ]

    async def _still_needs_attention(item: dict) -> Optional[dict]:
        status = await obs.get_pod_status(item["pod"], item["namespace"])
        if status.phase.value == "Unknown":
            return None  # pod no longer exists
        if status.phase.value == "Running" and status.ready:
            return None  # recovered on its own since the escalation
        return {**item, "current_phase": status.phase.value, "current_ready": status.ready}

    results = await asyncio.gather(*(_still_needs_attention(item) for item in candidates))
    items = [r for r in results if r is not None]
    items.sort(key=lambda i: i["detected_at"], reverse=True)
    return {"count": len(items), "items": items}


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

    `api_key` in the request body overrides the ANTHROPIC_API_KEY env var for
    this call only — lets the dashboard supply a key the user typed in the UI
    without it ever being baked into a cluster Secret.
    """
    key = req.api_key or API_KEY
    if not key:
        raise HTTPException(
            status_code=503,
            detail="No Anthropic API key available — set ANTHROPIC_API_KEY on the "
                   "deployment, or enter one in the dashboard's API key field.",
        )

    result = await run_l1_runbook(
        pod=req.pod,
        namespace=req.namespace,
        deployment=req.deployment,
        api_key=key,
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
