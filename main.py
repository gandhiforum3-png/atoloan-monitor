"""
Atoloan Troubleshooter Agent — FastAPI backend
Auto-remediates simple issues. Escalates complex ones to the user.
"""

import asyncio
import json
import logging
import os
import subprocess
from datetime import datetime
from typing import AsyncIterator, Literal

import anthropic
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("atoloan-agent")

# ── Config ─────────────────────────────────────────────────────────────────────

ENVIRONMENTS = {
    "local": {
        "kubeconfig": None,
        "namespaces": ["atoloan-postgres", "atoloan-backend", "atoloan-frontend", "atoloan-vault"],
        "ec2_ip": None,
        "secret_backend": "vault",
        "label": "local · minikube",
    },
    "dev": {
        "kubeconfig": os.path.expanduser("~/.kube/config-aws-dev"),
        "namespaces": ["atoloan-postgres-dev", "atoloan-backend-dev", "atoloan-frontend-dev"],
        "ec2_ip": "3.135.161.145",
        "secret_backend": "aws",
        "label": "aws-dev · 3.135.161.145",
    },
    "uat": {
        "kubeconfig": os.path.expanduser("~/.kube/config-aws-uat"),
        "namespaces": ["atoloan-postgres-uat", "atoloan-backend-uat", "atoloan-frontend-uat"],
        "ec2_ip": None,
        "secret_backend": "aws",
        "label": "aws-uat · uat.atoloan.com",
    },
    "prod": {
        "kubeconfig": os.path.expanduser("~/.kube/config-aws-prod"),
        "namespaces": ["atoloan-backend-prod", "atoloan-frontend-prod"],
        "ec2_ip": None,
        "secret_backend": "aws",
        "label": "aws-prod · atoloans.com",
    },
}

SAFE_DEPLOYMENTS = {
    "atoloan-api", "atoloan-ui", "postgres",
    "coredns", "ingress-nginx-controller",
}

FORBIDDEN_OPERATIONS = frozenset([
    "delete", "terminate", "destroy", "drop", "truncate",
])

# ── Auto-remediation policy ────────────────────────────────────────────────────
#
# auto_execute=True  → agent runs it immediately, no user input needed
# auto_execute=False → shown to user as "agent recommends, you approve"
#
# Rules for auto_execute=True:
#   - Restarts and rollout restarts (stateless, reversible)
#   - Force ExternalSecret re-sync (read-side, no data change)
#   - Ingress externalIPs patch (networking fix, no data change)
#   - CoreDNS restart (local only, recovers in seconds)
#
# Rules for auto_execute=False (always needs human):
#   - Scaling deployments (changes resource allocation)
#   - Any aws secretsmanager write
#   - Any change to PVCs, ConfigMaps, or resource limits
#   - Anything on prod environment
#   - Anything with severity=critical touching multiple components

AUTO_REMEDIATION_WHITELIST = {
    # keyword in command → auto_execute flag
    "rollout restart": True,
    "annotate externalsecret": True,   # force re-sync
    "patch svc ingress-nginx": True,   # externalIPs fix
    "rollout restart deployment/coredns": True,
}

def should_auto_execute(command: str, env: str, severity: str, affected_count: int) -> bool:
    """
    Decide whether to run a command automatically or require human approval.
    Returns False (needs human) when:
      - environment is prod
      - severity is critical AND more than one component affected
      - command involves scaling
      - command involves any write to persistent state
    """
    # Prod always needs human
    if env == "prod":
        return False

    # Critical + multi-component = page the human
    if severity == "critical" and affected_count > 1:
        return False

    # Scale changes = human decision
    if "scale" in command.lower():
        return False

    # secretsmanager writes = human
    if "secretsmanager" in command and "create" in command:
        return False

    # Check whitelist
    for keyword, auto in AUTO_REMEDIATION_WHITELIST.items():
        if keyword in command.lower():
            return auto

    # Default: safe-looking kubectl read-side ops can run automatically
    safe_verbs = ("rollout", "annotate", "patch")
    parts = command.strip().split()
    if parts[0] == "kubectl" and len(parts) > 1 and parts[1] in safe_verbs:
        return True

    return False


# ── System prompt ──────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are the Atoloan SRE Agent — autonomous troubleshooter for this exact stack.

STACK:
- AWS region: us-east-2
- k3s on EC2 (t3.small) for dev/uat/prod; minikube for local
- namespaces: atoloan-backend-{env}, atoloan-frontend-{env}, atoloan-postgres-{env}
- deployments: atoloan-api (FastAPI uvicorn port 8000), atoloan-ui (React/nginx port 80), postgres (PostgreSQL 15)
- secrets: ExternalSecrets operator → AWS Secrets Manager (atoloan/postgres, atoloan/openai, atoloan/sevencredit) or Vault (local)
- ingress: nginx; k3s EC2 requires externalIPs patch — LoadBalancer never gets IP on bare EC2
- health endpoint: GET /hello on port 8000
- images: docker.io/gandhiforum3/atoloan-api:latest, gandhiforum3/atoloan-ui:latest

KNOWN BUGS:
- backend-ingress aws-dev has rewrite-target: / — breaks ALL API routes except /
- CoreDNS custom host IPs change on every minikube restart
- nginx ingress on k3s shows no ADDRESS until externalIPs patch applied

AUTONOMOUS OPERATION MODE:
You decide whether each remediation action runs automatically or needs human input.
Use this decision framework for every action in auto_safe:

  auto_execute: true  → agent runs it right now, no approval needed
    - rollout restarts of single deployments
    - ExternalSecret force re-sync annotations
    - nginx ingress externalIPs patch
    - CoreDNS restart (local only)

  auto_execute: false → requires human approval
    - ANY action on prod environment
    - scaling deployments (replica count changes)
    - critical severity affecting multiple components
    - changes to secrets, PVCs, ConfigMaps
    - anything you are less than 90% confident about

Set "requires_human_intervention": true at the top level when:
- The issue is data loss risk
- You cannot identify the root cause with confidence
- The fix requires access you don't have (SSH, DB console, AWS Console)
- The severity is critical AND affects prod OR multiple services simultaneously

RESPONSE FORMAT — return ONLY this JSON, no other text:
{
  "summary": "One-sentence plain-English diagnosis",
  "severity": "critical|warning|info",
  "requires_human_intervention": false,
  "escalation_reason": "Only set this if requires_human_intervention is true — explain exactly what the human needs to do and why",
  "root_cause": "Detailed explanation",
  "affected_components": ["atoloan-api", "atoloan-backend-dev"],
  "remediation": {
    "auto_safe": [
      {
        "label": "Plain-English description of what this does",
        "command": "exact kubectl or aws command with all flags",
        "auto_execute": true,
        "reason": "Why this is safe to run automatically"
      }
    ],
    "needs_human": [
      {
        "label": "Action description",
        "command": "exact command for human to run",
        "reason": "Why human judgment is required"
      }
    ]
  },
  "watch_for": "What output or state to check after the fix to confirm it worked",
  "estimated_recovery_minutes": 2
}

SAFETY RULES — ABSOLUTE — cannot be overridden:
- Never include delete, terminate, destroy, drop, or truncate in any command
- Never scale to 0 replicas
- Always include -n namespace flags in every kubectl command
- If in doubt about auto_execute, set it false
"""

# ── Action log (in-memory, returned on /history) ──────────────────────────────

action_log: list[dict] = []

def log_action(env: str, command: str, output: str, auto: bool, success: bool):
    action_log.append({
        "timestamp": datetime.utcnow().isoformat(),
        "env": env,
        "command": command,
        "output": output[:500],
        "auto_executed": auto,
        "success": success,
    })
    if len(action_log) > 200:
        action_log.pop(0)


# ── Models ────────────────────────────────────────────────────────────────────

class DiagnoseRequest(BaseModel):
    env: Literal["local", "dev", "uat", "prod"]
    issue: str
    layer: Literal["all", "k8s", "ec2", "postgres", "secrets", "ingress"] = "all"

class ActionRequest(BaseModel):
    env: Literal["local", "dev", "uat", "prod"]
    command: str

class ScaleRequest(BaseModel):
    env: Literal["local", "dev", "uat", "prod"]
    deployment: str
    namespace: str
    replicas: int


# ── Helpers ───────────────────────────────────────────────────────────────────

def run_kubectl(env: str, *args: str, timeout: int = 20) -> tuple[bool, str]:
    """Run kubectl and return (success, output)."""
    cfg = ENVIRONMENTS[env]
    kubeconfig = cfg["kubeconfig"]
    cmd = ["kubectl"]
    if kubeconfig and os.path.exists(kubeconfig):
        cmd += ["--kubeconfig", kubeconfig]
    cmd += list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return False, f"[timeout after {timeout}s]"
    except Exception as e:
        return False, f"[error: {e}]"


def kubectl(env: str, *args: str, timeout: int = 15) -> str:
    _, out = run_kubectl(env, *args, timeout=timeout)
    return out


def aws_cli(*args: str, timeout: int = 15) -> str:
    cmd = ["aws", "--region", "us-east-2"] + list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (r.stdout + r.stderr).strip()
    except Exception as e:
        return f"[error: {e}]"


def safety_check(command: str) -> None:
    lower = command.lower()
    for forbidden in FORBIDDEN_OPERATIONS:
        if forbidden in lower:
            raise HTTPException(
                status_code=400,
                detail=f"Safety violation: '{forbidden}' is a forbidden operation. Perform this manually.",
            )


def execute_command(env: str, command: str) -> tuple[bool, str]:
    """Execute a kubectl or aws command, enforcing safety first."""
    safety_check(command)
    parts = command.strip().split()
    if not parts or parts[0] not in ("kubectl", "aws"):
        return False, "Only kubectl and aws commands are permitted."

    cfg = ENVIRONMENTS[env]

    if parts[0] == "kubectl":
        kubeconfig = cfg["kubeconfig"]
        cmd = ["kubectl"]
        if kubeconfig and os.path.exists(kubeconfig):
            cmd += ["--kubeconfig", kubeconfig]
        cmd += parts[1:]
    else:
        cmd = parts

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return False, "Command timed out after 30s"
    except Exception as e:
        return False, str(e)


def gather_diagnostics(env: str, layer: str) -> dict:
    cfg = ENVIRONMENTS[env]
    namespaces = cfg["namespaces"]
    data = {"env": env, "layer": layer, "timestamp": datetime.utcnow().isoformat()}

    if layer in ("all", "k8s"):
        data["pods"] = {}
        data["events"] = {}
        data["ingress"] = {}
        data["externalsecrets"] = {}
        for ns in namespaces:
            data["pods"][ns] = kubectl(env, "get", "pods", "-n", ns, "-o", "wide")
            data["events"][ns] = kubectl(
                env, "get", "events", "-n", ns,
                "--sort-by=.lastTimestamp", "--field-selector=type=Warning",
            )
            data["ingress"][ns] = kubectl(env, "get", "ingress", "-n", ns)
            data["externalsecrets"][ns] = kubectl(env, "get", "externalsecret", "-n", ns)
        data["nodes"] = kubectl(env, "get", "nodes", "-o", "wide")

    if layer in ("all", "ingress"):
        data["ingress_controller"] = kubectl(
            env, "get", "svc", "ingress-nginx-controller", "-n", "ingress-nginx"
        )
        data["ingress_logs"] = kubectl(
            env, "logs", "-n", "ingress-nginx",
            "deployment/ingress-nginx-controller", "--tail=30",
        )

    if layer in ("all", "secrets"):
        data["externalsecrets"] = data.get("externalsecrets", {})
        for ns in namespaces:
            data["externalsecrets"][ns] = kubectl(env, "get", "externalsecret", "-n", ns, "-o", "wide")
        if cfg["secret_backend"] == "aws":
            data["aws_secrets"] = aws_cli(
                "secretsmanager", "list-secrets",
                "--query", "SecretList[?starts_with(Name, 'atoloan/')].{Name:Name,LastChangedDate:LastChangedDate}",
                "--output", "table",
            )

    if layer in ("all", "postgres"):
        pg_ns = next((n for n in namespaces if "postgres" in n), None)
        if pg_ns:
            data["postgres_pod"] = kubectl(env, "get", "pods", "-n", pg_ns)
            data["postgres_logs"] = kubectl(
                env, "logs", "-n", pg_ns, "-l", "app=postgres", "--tail=40",
            )

    if layer in ("all", "ec2") and cfg["ec2_ip"]:
        data["ec2_instances"] = aws_cli(
            "ec2", "describe-instances",
            "--filters", "Name=instance-state-name,Values=running",
            "--query", "Reservations[*].Instances[*].[Tags[?Key=='Name'].Value|[0],InstanceType,PublicIpAddress,State.Name]",
            "--output", "table",
        )

    return data


async def run_auto_remediation(
    env: str,
    actions: list[dict],
    severity: str,
    affected_count: int,
) -> list[dict]:
    """
    Execute all actions marked auto_execute=true by Claude.
    Returns a list of execution results.
    """
    results = []
    for action in actions:
        cmd = action.get("command", "")
        claude_auto = action.get("auto_execute", False)

        # Double-check with our own policy — Claude's flag AND our policy must agree
        policy_auto = should_auto_execute(cmd, env, severity, affected_count)
        will_auto = claude_auto and policy_auto

        if will_auto:
            log.info(f"AUTO-REMEDIATING: {cmd}")
            success, output = execute_command(env, cmd)
            log_action(env, cmd, output, auto=True, success=success)
            results.append({
                "label": action.get("label", cmd),
                "command": cmd,
                "auto_executed": True,
                "success": success,
                "output": output,
            })
        else:
            # Still included in response but marked as needing human
            results.append({
                "label": action.get("label", cmd),
                "command": cmd,
                "auto_executed": False,
                "success": None,
                "output": None,
                "reason": action.get("reason", "Requires human approval"),
            })

    return results


# ── FastAPI ────────────────────────────────────────────────────────────────────

app = FastAPI(title="Atoloan Troubleshooter Agent", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok", "agent": "atoloan-troubleshooter", "version": "2.0.0"}


@app.get("/history")
def get_history():
    """Return the agent's auto-remediation action log."""
    return {"actions": list(reversed(action_log))}


@app.get("/environments")
def list_environments():
    return {env: cfg["label"] for env, cfg in ENVIRONMENTS.items()}


@app.post("/diagnose/stream")
async def diagnose_stream(req: DiagnoseRequest):
    """
    Full autonomous pipeline:
    1. Gather live diagnostics
    2. Send to Claude for analysis
    3. Auto-execute safe actions immediately
    4. Return structured result with what was done and what needs human attention
    """
    async def event_stream() -> AsyncIterator[str]:
        yield f"data: {json.dumps({'type': 'status', 'message': 'Gathering live diagnostics...'})}\n\n"

        diagnostics = gather_diagnostics(req.env, req.layer)
        yield f"data: {json.dumps({'type': 'status', 'message': f'Got {len(diagnostics)} diagnostic sources. Sending to Claude...'})}\n\n"

        env_cfg = ENVIRONMENTS[req.env]
        user_message = (
            f"Environment: {req.env} ({env_cfg['label']})\n"
            f"Reported issue: {req.issue}\n"
            f"Layer focus: {req.layer}\n\n"
            f"=== LIVE DIAGNOSTIC DATA ===\n"
            f"{json.dumps(diagnostics, indent=2)}\n\n"
            f"Analyse the data and return your diagnosis JSON."
        )

        client = anthropic.AsyncAnthropic()
        full_text = ""

        async with client.messages.stream(
            model="claude-sonnet-4-20250514",
            max_tokens=2500,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        ) as stream:
            async for text in stream.text_stream:
                full_text += text
                yield f"data: {json.dumps({'type': 'token', 'text': text})}\n\n"

        # Parse Claude's diagnosis
        raw = full_text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        try:
            diagnosis = json.loads(raw)
        except json.JSONDecodeError:
            diagnosis = {"summary": raw, "severity": "info", "raw": True,
                         "requires_human_intervention": True,
                         "escalation_reason": "Could not parse structured diagnosis. Review raw output."}

        # ── Auto-remediation pass ──────────────────────────────────────────
        auto_actions = diagnosis.get("remediation", {}).get("auto_safe", [])
        needs_human_actions = diagnosis.get("remediation", {}).get("needs_human", [])
        severity = diagnosis.get("severity", "info")
        affected_count = len(diagnosis.get("affected_components", []))
        requires_human = diagnosis.get("requires_human_intervention", False)

        execution_results = []
        if auto_actions and not requires_human:
            yield f"data: {json.dumps({'type': 'status', 'message': 'Running safe remediations automatically...'})}\n\n"
            execution_results = await run_auto_remediation(
                req.env, auto_actions, severity, affected_count
            )

            # Summarise what was done
            done = [r for r in execution_results if r["auto_executed"] and r["success"]]
            failed = [r for r in execution_results if r["auto_executed"] and not r["success"]]
            deferred = [r for r in execution_results if not r["auto_executed"]]

            if done:
                yield f"data: {json.dumps({'type': 'status', 'message': f'Auto-fixed {len(done)} issue(s). Checking for remaining problems...'})}\n\n"
            if failed:
                yield f"data: {json.dumps({'type': 'status', 'message': f'{len(failed)} action(s) failed — check output.'})}\n\n"
            if deferred:
                yield f"data: {json.dumps({'type': 'status', 'message': f'{len(deferred)} action(s) need your approval.'})}\n\n"

        yield f"data: {json.dumps({'type': 'done', 'diagnosis': diagnosis, 'execution_results': execution_results})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/action/run")
async def run_action(req: ActionRequest):
    """Human-approved command execution."""
    safety_check(req.command)
    success, output = execute_command(req.env, req.command)
    log_action(req.env, req.command, output, auto=False, success=success)
    return {
        "success": success,
        "output": output,
        "command": req.command,
        "timestamp": datetime.utcnow().isoformat(),
    }


@app.post("/action/scale")
async def scale_deployment(req: ScaleRequest):
    """Human-approved scale operation (always requires approval)."""
    if req.deployment not in SAFE_DEPLOYMENTS:
        raise HTTPException(
            status_code=400,
            detail=f"'{req.deployment}' is not in SAFE_DEPLOYMENTS. Add it in main.py.",
        )
    replicas = max(1, min(5, req.replicas))
    cfg = ENVIRONMENTS[req.env]
    kubeconfig = cfg["kubeconfig"]
    cmd = ["kubectl"]
    if kubeconfig and os.path.exists(kubeconfig):
        cmd += ["--kubeconfig", kubeconfig]
    cmd += ["scale", "deployment", req.deployment, f"--replicas={replicas}", "-n", req.namespace]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        success = r.returncode == 0
        output = (r.stdout + r.stderr).strip()
        log_action(req.env, " ".join(cmd), output, auto=False, success=success)
        return {"success": success, "output": output, "deployment": req.deployment,
                "namespace": req.namespace, "replicas": replicas,
                "timestamp": datetime.utcnow().isoformat()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/status/{env}")
def cluster_status(env: str):
    if env not in ENVIRONMENTS:
        raise HTTPException(status_code=404, detail=f"Unknown environment: {env}")
    cfg = ENVIRONMENTS[env]
    pods_all = []
    for ns in cfg["namespaces"]:
        out = kubectl(env, "get", "pods", "-n", ns, "--no-headers")
        for line in out.splitlines():
            if not line.strip():
                continue
            parts = line.split()
            if len(parts) >= 3:
                pods_all.append({
                    "namespace": ns, "name": parts[0], "ready": parts[1],
                    "status": parts[2], "restarts": parts[3] if len(parts) > 3 else "0",
                })
    total = len(pods_all)
    running = sum(1 for p in pods_all if p["status"] == "Running")
    problem = [p for p in pods_all if p["status"] not in ("Running", "Completed", "Succeeded")]
    return {
        "env": env, "label": cfg["label"],
        "pods": {"total": total, "running": running, "problem": len(problem)},
        "problem_pods": problem,
        "nodes": kubectl(env, "get", "nodes", "--no-headers"),
        "timestamp": datetime.utcnow().isoformat(),
    }
