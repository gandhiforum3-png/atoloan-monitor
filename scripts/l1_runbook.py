"""
l1_runbook.py — Structured L1 troubleshooting decision tree for Kubernetes pods.

The runbook is a fixed flowchart — not open-ended LLM reasoning. It follows
deterministic steps for each failure class, attempts safe remediations, verifies
the fix, and builds an escalation packet when it cannot resolve the issue.

Claude is used exactly once: to generate the recommended L2 actions in the
escalation packet. Everything else is deterministic Python.

Entry point:
    result = await run_l1_runbook(pod, namespace, deployment)

Decision tree:
    Observe → Classify → Remediate → Verify → Resolve or Escalate

Failure classes handled at L1:
    OOMKilled            → patch_memory_limit (if safe) → verify
    CrashLoopBackOff     → restart_pod → verify
    ImagePullBackOff     → force_image_repull → verify (or escalate if tag is bad)
    Pending/Scheduling   → escalate (resource/affinity fixes are not L1)
    Stuck Terminating    → delete_stuck_pod (explicit force)
    Probe failure        → wait + recheck → escalate if still failing
    Healthy              → resolve immediately

Usage:
    import asyncio
    from l1_runbook import run_l1_runbook

    result = asyncio.run(
        run_l1_runbook(pod="api-6b9df7-xzp2k", namespace="prod", deployment="api")
    )
    print(result.resolution)          # "resolved" | "escalated" | "needs_human"
    print(result.summary)
    if result.escalation_packet:
        print(result.escalation_packet.model_dump_json(indent=2))
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from typing import Optional

import anthropic

import pod_observer as obs
import remediators as rem
from models import (
    CrashDiagnosis,
    EscalationPacket,
    FailureClass,
    L1Resolution,
    PodPhase,
    PodStatus,
    RemediationResult,
    RunbookResult,
    RunbookStep,
    Severity,
)


# ---------------------------------------------------------------------------
# Step builder helper
# ---------------------------------------------------------------------------

_step_counter: int = 0


def _step(action: str, finding: str, outcome: str) -> RunbookStep:
    global _step_counter
    _step_counter += 1
    return RunbookStep(
        step_num=_step_counter,
        action=action,
        finding=finding,
        outcome=outcome,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


def _reset_steps() -> None:
    global _step_counter
    _step_counter = 0


# ---------------------------------------------------------------------------
# Escalation packet builder
# ---------------------------------------------------------------------------

async def _build_escalation_packet(
    pod: str,
    namespace: str,
    severity: Severity,
    failure_class: str,
    summary: str,
    steps: list[RunbookStep],
    remediations: list[RemediationResult],
    status: Optional[PodStatus],
    evidence: dict,
    api_key: Optional[str] = None,
) -> EscalationPacket:
    """
    Build a structured L2 handoff packet.
    Claude generates the recommended_l2_actions list based on the evidence.
    """
    l2_actions = await _generate_l2_actions(
        pod=pod,
        namespace=namespace,
        failure_class=failure_class,
        summary=summary,
        evidence=evidence,
        steps=steps,
        remediations=remediations,
        api_key=api_key,
    )

    return EscalationPacket(
        pod=pod,
        namespace=namespace,
        severity=severity,
        created_at=datetime.now(timezone.utc).isoformat(),
        failure_class=failure_class,
        summary=summary,
        steps_taken=steps,
        remediations_attempted=remediations,
        current_pod_state=status.model_dump() if status else None,
        evidence=evidence,
        recommended_l2_actions=l2_actions,
    )


async def _generate_l2_actions(
    pod: str,
    namespace: str,
    failure_class: str,
    summary: str,
    evidence: dict,
    steps: list[RunbookStep],
    remediations: list[RemediationResult],
    api_key: Optional[str] = None,
) -> list[str]:
    """
    Ask Claude to generate a concise list of recommended L2 actions
    based on the full context of what L1 tried and found.
    Returns a list of action strings.
    """
    try:
        client = anthropic.AsyncAnthropic(
            api_key=api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        )
        if not client.api_key:
            return _fallback_l2_actions(failure_class)

        steps_text = "\n".join(
            f"  {s.step_num}.[{s.outcome}] {s.action}: {s.finding}"
            for s in steps
        )
        rem_text = "\n".join(
            f"  {r.action}/{r.target}: {'ok' if r.success else 'fail'} {r.message}"
            for r in remediations
        ) or "  none"

        evidence_text = _format_evidence(evidence)

        prompt = f"""SRE escalating a K8s pod issue L1->L2.

Pod: {pod} | ns: {namespace} | failure: {failure_class}
Summary: {summary}

Steps:
{steps_text}

Remediations:
{rem_text}

Evidence:
{evidence_text}

List 3-6 concrete L2 actions, kubectl commands where relevant. Numbered list only, nothing else."""

        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )

        text = response.content[0].text.strip()
        # Parse numbered list
        actions = []
        for line in text.splitlines():
            line = line.strip()
            if line and (line[0].isdigit() or line.startswith("-")):
                # Strip leading "1. " or "- "
                clean = line.lstrip("0123456789.-) ").strip()
                if clean:
                    actions.append(clean)
        return actions if actions else _fallback_l2_actions(failure_class)

    except Exception:
        return _fallback_l2_actions(failure_class)


def _format_evidence(evidence: dict, max_chars: int = 250) -> str:
    """
    Flatten evidence into compact 'key: value' lines instead of pretty-printed
    JSON. Avoids spending prompt tokens on braces/quotes/indentation and on
    Python's list repr (brackets, escaped quotes) for list-valued fields.
    """
    lines = []
    for key, value in evidence.items():
        if isinstance(value, list):
            text = "; ".join(str(v) for v in value)
        elif isinstance(value, dict):
            text = json.dumps(value, separators=(",", ":"))
        else:
            text = str(value)
        lines.append(f"{key}: {text[:max_chars]}")
    return "\n".join(lines)


def _fallback_l2_actions(failure_class: str) -> list[str]:
    """Static fallback when Claude is unavailable."""
    base = [
        "Review kubectl describe pod output for the affected pod",
        "Check application logs from the last 24 hours for patterns",
        "Verify recent deployments with: kubectl rollout history deployment/<name>",
    ]
    specific = {
        "OOMKilled": [
            "Profile memory usage under load (heap dump if Java/Node, pprof if Go)",
            "Review memory limits vs actual working set across all environments",
            "Consider enabling VPA for automatic limit tuning",
        ],
        "CrashLoopBackOff": [
            "Check for environment variable or secret changes that correlate with crashes",
            "Review application startup sequence for dependency ordering issues",
            "Enable debug logging and capture a full crash cycle",
        ],
        "ImagePullError": [
            "Verify the image tag exists: docker manifest inspect <image>:<tag>",
            "Check imagePullSecrets are correctly set and not expired",
            "Review registry access logs for auth errors",
        ],
        "FailedScheduling": [
            "Run: kubectl describe nodes to check allocatable resources",
            "Review node affinity/taints against pod tolerations",
            "Consider adding nodes to the cluster or right-sizing resource requests",
        ],
    }
    return base + specific.get(failure_class, [])


# ---------------------------------------------------------------------------
# Phase-specific runbook branches
# ---------------------------------------------------------------------------

async def _handle_crash_loop(
    pod: str,
    namespace: str,
    deployment: Optional[str],
    steps: list[RunbookStep],
    remediations: list[RemediationResult],
    status: PodStatus,
) -> tuple[L1Resolution, Severity, str, dict]:
    """Handle CrashLoopBackOff and OOMKilled."""

    # Diagnose crash (parallel gather under the hood)
    diagnosis = await obs.diagnose_crash(pod, namespace)
    steps.append(_step(
        "diagnose_crash",
        f"exit_code={diagnosis.exit_code}, reason={diagnosis.kill_reason}, "
        f"restarts={diagnosis.restart_count}",
        "pass",
    ))

    evidence = {
        "failure_class":  diagnosis.failure_class.value,
        "exit_code":      diagnosis.exit_code,
        "kill_reason":    diagnosis.kill_reason,
        "restart_count":  diagnosis.restart_count,
        "previous_logs":  diagnosis.previous_logs[-20:],
        "memory_limit":   diagnosis.memory_limit,
        "memory_usage":   diagnosis.memory_usage,
        "events":         [e.model_dump() for e in diagnosis.recent_events[:5]],
    }

    # --- OOMKilled branch ---
    if diagnosis.failure_class == FailureClass.OOM_KILLED:
        if diagnosis.memory_limit and deployment:
            result = await rem.patch_memory_limit(
                deployment=deployment,
                namespace=namespace,
                increase_pct=50.0,
                dry_run=False,
            )
            remediations.append(result)
            steps.append(_step(
                "patch_memory_limit",
                result.message,
                "remediated" if result.success else "fail",
            ))

            if result.success:
                # Verify
                await asyncio.sleep(30)
                healthy, msg = await rem.verify_pod_healthy(
                    pod_prefix=pod.rsplit("-", 2)[0],
                    namespace=namespace,
                    timeout_s=120,
                )
                steps.append(_step("verify_pod_healthy", msg, "pass" if healthy else "fail"))

                if healthy:
                    return (
                        L1Resolution.RESOLVED, Severity.P2,
                        f"OOMKilled resolved — memory limit increased. {result.message}",
                        evidence,
                    )

            return (
                L1Resolution.ESCALATED, Severity.P2,
                f"OOMKilled — memory patch {'applied but pod still unhealthy' if result.success else 'failed'}. "
                f"Memory: limit={diagnosis.memory_limit}, usage={diagnosis.memory_usage}.",
                evidence,
            )
        else:
            steps.append(_step(
                "patch_memory_limit",
                "Skipped — no deployment name provided or no existing limit to increase",
                "escalate",
            ))
            return (
                L1Resolution.ESCALATED, Severity.P2,
                f"OOMKilled — cannot auto-patch without deployment name. "
                f"Memory limit: {diagnosis.memory_limit}, usage: {diagnosis.memory_usage}.",
                evidence,
            )

    # --- Exit 127 (binary not found) — image issue, L1 cannot fix ---
    if diagnosis.exit_code == 127:
        steps.append(_step(
            "classify_exit_code",
            "Exit 127 — entrypoint binary not found in image. Image issue.",
            "escalate",
        ))
        return (
            L1Resolution.ESCALATED, Severity.P1,
            "Exit 127: entrypoint binary not found. Image is broken — needs L2 to fix Dockerfile and rebuild.",
            evidence,
        )

    # --- Exit 126 (permission) ---
    if diagnosis.exit_code == 126:
        steps.append(_step(
            "classify_exit_code",
            "Exit 126 — entrypoint not executable (permission denied).",
            "escalate",
        ))
        return (
            L1Resolution.ESCALATED, Severity.P2,
            "Exit 126: entrypoint permission denied. Needs L2 to fix Dockerfile (chmod +x).",
            evidence,
        )

    # --- CrashLoopBackOff with exit 1 (app error) — try restart ---
    if deployment:
        result = await rem.restart_pod(pod=pod, namespace=namespace)
        remediations.append(result)
        steps.append(_step(
            "restart_pod",
            result.message,
            "remediated" if result.success else "fail",
        ))

        if result.success:
            await asyncio.sleep(40)
            healthy, msg = await rem.verify_pod_healthy(
                pod_prefix=pod.rsplit("-", 2)[0],
                namespace=namespace,
                timeout_s=120,
            )
            steps.append(_step("verify_pod_healthy", msg, "pass" if healthy else "fail"))

            if healthy:
                return (
                    L1Resolution.RESOLVED, Severity.P2,
                    f"CrashLoopBackOff resolved by pod restart. {msg}",
                    evidence,
                )

    return (
        L1Resolution.ESCALATED, Severity.P2,
        f"CrashLoopBackOff (exit {diagnosis.exit_code}) — restart {'did not resolve' if remediations else 'not attempted (no deployment)'}. "
        f"Check previous logs: {diagnosis.previous_logs[-3:]}",
        evidence,
    )


async def _handle_image_pull(
    pod: str,
    namespace: str,
    deployment: Optional[str],
    steps: list[RunbookStep],
    remediations: list[RemediationResult],
    status: PodStatus,
) -> tuple[L1Resolution, Severity, str, dict]:
    """Handle ImagePullBackOff."""

    # Check if it's a tag issue or transient
    events = await obs.get_pod_events(pod, namespace)
    pull_errors = [e for e in events if "pull" in e.reason.lower() or "image" in e.reason.lower()]

    # "manifest unknown" or "not found" → bad tag, L1 cannot fix
    is_bad_tag = any(
        kw in (e.message.lower()) for e in pull_errors
        for kw in ("manifest unknown", "not found", "does not exist", "invalid reference")
    )

    evidence = {
        "pull_events": [e.model_dump() for e in pull_errors[:3]],
        "image": status.container_statuses[0].image if status.container_statuses else "unknown",
    }

    if is_bad_tag:
        steps.append(_step(
            "classify_image_pull",
            "Image tag not found in registry — cannot auto-fix.",
            "escalate",
        ))
        return (
            L1Resolution.ESCALATED, Severity.P1,
            "ImagePullBackOff — image tag does not exist. L2 must fix image reference and redeploy.",
            evidence,
        )

    # Transient pull error — try a rollout restart
    if deployment:
        result = await rem.force_image_repull(deployment=deployment, namespace=namespace)
        remediations.append(result)
        steps.append(_step(
            "force_image_repull",
            result.message,
            "remediated" if result.success else "fail",
        ))

        if result.success:
            await asyncio.sleep(30)
            healthy, msg = await rem.verify_pod_healthy(
                pod_prefix=pod.rsplit("-", 2)[0],
                namespace=namespace,
                timeout_s=120,
            )
            steps.append(_step("verify_pod_healthy", msg, "pass" if healthy else "fail"))
            if healthy:
                return (
                    L1Resolution.RESOLVED, Severity.P3,
                    f"ImagePullBackOff resolved by rollout restart (transient registry issue). {msg}",
                    evidence,
                )

    return (
        L1Resolution.ESCALATED, Severity.P2,
        "ImagePullBackOff — rollout restart did not resolve. Registry auth or network issue likely.",
        evidence,
    )


async def _handle_pending(
    pod: str,
    namespace: str,
    steps: list[RunbookStep],
    status: PodStatus,
) -> tuple[L1Resolution, Severity, str, dict]:
    """Handle Pending pods — always escalate (scheduling fixes are not L1)."""

    diagnosis = await obs.diagnose_pending(pod, namespace)
    steps.append(_step(
        "diagnose_pending",
        diagnosis.summary[:200],
        "escalate",
    ))

    evidence = {
        "scheduling_events": [e.model_dump() for e in diagnosis.scheduling_events[:5]],
        "node_pressure":     diagnosis.node_pressure_nodes,
        "unbound_pvcs":      diagnosis.unbound_pvcs,
        "resource_requests": [r.model_dump() for r in diagnosis.resource_requests],
    }

    severity = Severity.P1 if diagnosis.node_pressure_nodes else Severity.P2

    return (
        L1Resolution.ESCALATED,
        severity,
        f"Pod stuck in Pending — L1 cannot fix scheduling issues. {diagnosis.summary}",
        evidence,
    )


async def _handle_probe_failure(
    pod: str,
    namespace: str,
    steps: list[RunbookStep],
    status: PodStatus,
) -> tuple[L1Resolution, Severity, str, dict]:
    """Handle running-but-not-ready (probe failures)."""

    events = await obs.get_pod_events(pod, namespace)
    probe_events = [e for e in events if "probe" in e.reason.lower() or "probe" in e.message.lower()]

    steps.append(_step(
        "check_probe_events",
        f"{len(probe_events)} probe failure events found",
        "pass" if probe_events else "fail",
    ))

    # Wait 30s and recheck — transient probe failures resolve on their own
    steps.append(_step("wait_recheck", "Waiting 30s for transient probe failure to self-heal…", "pass"))
    await asyncio.sleep(30)

    new_status = await obs.get_pod_status(pod, namespace)
    if new_status.ready:
        steps.append(_step("verify_ready", "Pod became Ready after waiting.", "pass"))
        return (
            L1Resolution.RESOLVED, Severity.P3,
            "Probe failure was transient — pod recovered without intervention.",
            {"probe_events": [e.model_dump() for e in probe_events[:3]]},
        )

    steps.append(_step("verify_ready", "Pod still not Ready after 30s wait.", "escalate"))
    evidence = {
        "probe_events": [e.model_dump() for e in probe_events[:5]],
        "conditions":   new_status.conditions,
        "container_states": [cs.model_dump() for cs in new_status.container_statuses],
    }

    return (
        L1Resolution.ESCALATED, Severity.P2,
        "Pod stuck not-Ready — probe configuration or app health endpoint needs L2 review.",
        evidence,
    )


async def _handle_stuck_terminating(
    pod: str,
    namespace: str,
    steps: list[RunbookStep],
    remediations: list[RemediationResult],
    status: PodStatus,
) -> tuple[L1Resolution, Severity, str, dict]:
    """Handle stuck Terminating pods — needs explicit operator approval."""

    steps.append(_step(
        "detect_stuck_terminating",
        "Pod is stuck in Terminating state. Requires explicit force-delete approval.",
        "needs_human",
    ))

    evidence = {
        "phase":    status.phase.value,
        "conditions": status.conditions,
    }

    return (
        L1Resolution.NEEDS_HUMAN, Severity.P3,
        "Pod stuck in Terminating — force-delete requires human approval. "
        "Run: kubectl delete pod <pod> --force --grace-period=0",
        evidence,
    )


# ---------------------------------------------------------------------------
# Main L1 runbook
# ---------------------------------------------------------------------------

async def run_l1_runbook(
    pod: str,
    namespace: str = "default",
    deployment: Optional[str] = None,
    api_key: Optional[str] = None,
) -> RunbookResult:
    """
    Execute the full L1 troubleshooting decision tree for a Kubernetes pod.

    Args:
        pod:        Pod name to investigate and remediate
        namespace:  Kubernetes namespace
        deployment: Owning Deployment name (enables rollback, scale, memory patch).
                    If not provided, remediations that require a Deployment are skipped.
        api_key:    Anthropic API key for escalation packet L2 guidance generation.
                    Falls back to ANTHROPIC_API_KEY env var.

    Returns:
        RunbookResult with resolution, all steps taken, remediations attempted,
        and (if escalated) a fully populated EscalationPacket.
    """
    _reset_steps()
    t0 = time.monotonic()
    steps: list[RunbookStep] = []
    remediations: list[RemediationResult] = []

    # ── Step 0: Observe current state ────────────────────────────────────────
    status = await obs.get_pod_status(pod, namespace)
    steps.append(_step(
        "get_pod_status",
        f"phase={status.phase.value}, ready={status.ready}, "
        f"restarts={status.total_restarts}, hint={status.failure_hint}",
        "pass",
    ))

    # ── Gate: is it already healthy? ─────────────────────────────────────────
    if status.phase == PodPhase.SUCCEEDED:
        return RunbookResult(
            pod=pod, namespace=namespace,
            resolution=L1Resolution.RESOLVED,
            severity=Severity.P3,
            failure_class=FailureClass.HEALTHY.value,
            steps=steps, remediations=[],
            resolved_in_s=round(time.monotonic() - t0, 2),
            summary="Pod completed successfully — no action needed.",
        )

    if status.phase == PodPhase.RUNNING and status.ready:
        return RunbookResult(
            pod=pod, namespace=namespace,
            resolution=L1Resolution.RESOLVED,
            severity=Severity.P3,
            failure_class=FailureClass.HEALTHY.value,
            steps=steps, remediations=[],
            resolved_in_s=round(time.monotonic() - t0, 2),
            summary="Pod is healthy and ready — no action needed.",
        )

    # ── Classify failure mode ─────────────────────────────────────────────────
    failure_class = FailureClass.UNKNOWN
    waiting_reason = ""

    for cs in status.container_statuses:
        if cs.waiting and cs.waiting.reason:
            waiting_reason = cs.waiting.reason
        if cs.last_state and cs.last_state.reason == "OOMKilled":
            failure_class = FailureClass.OOM_KILLED
        elif cs.waiting and cs.waiting.reason == "CrashLoopBackOff":
            failure_class = FailureClass.CRASH_LOOP
        elif cs.waiting and "ImagePull" in (cs.waiting.reason or ""):
            failure_class = FailureClass.IMAGE_PULL

    if status.phase == PodPhase.PENDING:
        failure_class = FailureClass.PENDING_SCHEDULE
    elif status.phase == PodPhase.RUNNING and not status.ready and failure_class == FailureClass.UNKNOWN:
        failure_class = FailureClass.PROBE_FAILURE

    steps.append(_step(
        "classify_failure",
        f"failure_class={failure_class.value}, waiting_reason='{waiting_reason}'",
        "pass",
    ))

    # ── Stuck Terminating ─────────────────────────────────────────────────────
    if waiting_reason == "Terminating":
        resolution, severity, summary, evidence = await _handle_stuck_terminating(
            pod, namespace, steps, remediations, status
        )

    # ── CrashLoopBackOff / OOMKilled ─────────────────────────────────────────
    elif failure_class in (FailureClass.CRASH_LOOP, FailureClass.OOM_KILLED):
        resolution, severity, summary, evidence = await _handle_crash_loop(
            pod, namespace, deployment, steps, remediations, status
        )

    # ── ImagePullBackOff ──────────────────────────────────────────────────────
    elif failure_class == FailureClass.IMAGE_PULL:
        resolution, severity, summary, evidence = await _handle_image_pull(
            pod, namespace, deployment, steps, remediations, status
        )

    # ── Pending / scheduling failure ─────────────────────────────────────────
    elif failure_class == FailureClass.PENDING_SCHEDULE:
        resolution, severity, summary, evidence = await _handle_pending(
            pod, namespace, steps, status
        )

    # ── Probe failure (running, not ready) ────────────────────────────────────
    elif failure_class == FailureClass.PROBE_FAILURE:
        resolution, severity, summary, evidence = await _handle_probe_failure(
            pod, namespace, steps, status
        )

    # ── Unknown / catch-all ──────────────────────────────────────────────────
    else:
        events = await obs.get_namespace_events(namespace, warnings_only=True, limit=10)
        steps.append(_step(
            "collect_events",
            f"{len(events)} namespace warning events",
            "escalate",
        ))
        evidence = {
            "pod_phase": status.phase.value,
            "container_statuses": [cs.model_dump() for cs in status.container_statuses],
            "events": [e.model_dump() for e in events[:5]],
        }
        resolution = L1Resolution.ESCALATED
        severity   = Severity.P2
        summary    = f"Unknown failure pattern (phase={status.phase.value}). L1 cannot classify — escalating."

    # ── Build escalation packet if needed ────────────────────────────────────
    escalation_packet = None
    if resolution in (L1Resolution.ESCALATED, L1Resolution.NEEDS_HUMAN):
        escalation_packet = await _build_escalation_packet(
            pod=pod,
            namespace=namespace,
            severity=severity,
            failure_class=failure_class.value,
            summary=summary,
            steps=steps,
            remediations=remediations,
            status=status,
            evidence=evidence,
            api_key=api_key,
        )

    return RunbookResult(
        pod=pod,
        namespace=namespace,
        resolution=resolution,
        severity=severity,
        failure_class=failure_class.value,
        steps=steps,
        remediations=remediations,
        escalation_packet=escalation_packet,
        resolved_in_s=round(time.monotonic() - t0, 2),
        summary=summary,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    async def _main():
        if len(sys.argv) < 2:
            print("Usage: python l1_runbook.py <pod> [namespace] [deployment]")
            sys.exit(1)

        pod        = sys.argv[1]
        namespace  = sys.argv[2] if len(sys.argv) > 2 else "default"
        deployment = sys.argv[3] if len(sys.argv) > 3 else None

        print(f"[L1 RUNBOOK] pod={pod} ns={namespace} deployment={deployment or 'not provided'}\n")

        result = await run_l1_runbook(pod, namespace, deployment)

        print(f"RESOLUTION  : {result.resolution.value.upper()}")
        print(f"SEVERITY    : {result.severity.value}")
        print(f"FAILURE     : {result.failure_class}")
        print(f"SUMMARY     : {result.summary}")
        print(f"DURATION    : {result.resolved_in_s}s")
        print(f"\nSTEPS ({len(result.steps)}):")
        for s in result.steps:
            icon = {"pass": "✓", "fail": "✗", "remediated": "⚙", "escalate": "↑", "needs_human": "👤"}.get(s.outcome, "·")
            print(f"  {icon} [{s.step_num}] {s.action}: {s.finding}")

        if result.remediations:
            print(f"\nREMEDIATIONS ({len(result.remediations)}):")
            for r in result.remediations:
                icon = "✓" if r.success else "✗"
                print(f"  {icon} {r.action}: {r.message}")

        if result.escalation_packet:
            pkt = result.escalation_packet
            print(f"\nESCALATION PACKET [{pkt.severity.value}]")
            print(f"  {pkt.summary}")
            print(f"\n  L2 RECOMMENDED ACTIONS:")
            for i, action in enumerate(pkt.recommended_l2_actions, 1):
                print(f"    {i}. {action}")

    asyncio.run(_main())
