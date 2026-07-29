"""
remediation_agent.py — Fix stage of the standalone remediation pipeline.

Consumes the IssueReport produced by orchestrator.find_issue() and decides
whether/how to act on it, using the Action Catalog (see
REMEDIATION_AGENT_DESIGN.md). Deliberately independent of l1_runbook.py —
this is a separate, on-demand pipeline: remediate.py -> find_issue() -> dispatch().

Safety model — same discipline as l1_runbook.py, just applied to an
open-vocabulary classifier instead of a fixed 5-class ladder:
  - The orchestrator's CLASSIFICATION/CONFIDENCE is a *proposal*, never a command.
  - Every catalog entry has a confirm function that independently re-derives
    the evidence via a deterministic pod_observer.py call. If the confirm
    check doesn't corroborate the classification, dispatch() downgrades to
    Tier 3 (diagnose-only) regardless of how confident the orchestrator was.
  - Tier 1 executes immediately once confirmed. Tier 2 executes only with
    auto_approve=True. Tier 3 never executes.
  - remediators.py's FORBIDDEN_OPERATIONS guard still applies underneath —
    this module never bypasses it.

Entry point:
    summary = await dispatch(issue_report, dry_run=False, auto_approve=False)
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

import anthropic

import pod_observer as obs
import remediators as rem
from models import (
    ActionTier,
    IssueReport,
    RemediationResolution,
    RemediationResult,
    RemediationSummary,
)


# ---------------------------------------------------------------------------
# Confirm functions — independently re-derive evidence for one classification
# family each. Return (confirmed: bool, evidence: dict, note: str).
# ---------------------------------------------------------------------------

ConfirmResult = tuple[bool, dict, str]


async def _confirm_oom_killed(issue: IssueReport) -> ConfirmResult:
    d = await obs.diagnose_crash(issue.pod, issue.namespace)
    confirmed = d.failure_class.value == "OOMKilled" and bool(d.memory_limit)
    return confirmed, d.model_dump(), d.summary


async def _confirm_crash_transient(issue: IssueReport) -> ConfirmResult:
    d = await obs.diagnose_crash(issue.pod, issue.namespace)
    confirmed = d.exit_code not in (126, 127) and d.failure_class.value in ("CrashLoopBackOff", "Unknown")
    return confirmed, d.model_dump(), d.summary


async def _confirm_crash_exit_127(issue: IssueReport) -> ConfirmResult:
    d = await obs.diagnose_crash(issue.pod, issue.namespace)
    return d.exit_code == 127, d.model_dump(), d.summary


async def _confirm_crash_exit_126(issue: IssueReport) -> ConfirmResult:
    d = await obs.diagnose_crash(issue.pod, issue.namespace)
    return d.exit_code == 126, d.model_dump(), d.summary


async def _confirm_init_crash_transient(issue: IssueReport) -> ConfirmResult:
    d = await obs.diagnose_init_container_crash(issue.pod, issue.namespace)
    confirmed = d.exit_code is not None and d.exit_code not in (126, 127)
    return confirmed, d.model_dump(), d.summary


async def _confirm_init_crash_bad_exit(issue: IssueReport) -> ConfirmResult:
    d = await obs.diagnose_init_container_crash(issue.pod, issue.namespace)
    return d.exit_code in (126, 127), d.model_dump(), d.summary


_BAD_TAG_KEYWORDS = ("manifest unknown", "not found", "does not exist", "invalid reference")


async def _confirm_image_pull_transient(issue: IssueReport) -> ConfirmResult:
    events = await obs.get_pod_events(issue.pod, issue.namespace)
    pull_events = [e for e in events if "pull" in e.reason.lower() or "image" in e.reason.lower()]
    is_bad_tag = any(kw in e.message.lower() for e in pull_events for kw in _BAD_TAG_KEYWORDS)
    evidence = {"pull_events": [e.model_dump() for e in pull_events[:5]]}
    note = "Bad image tag" if is_bad_tag else "No bad-tag signature in pull events — treating as transient."
    return not is_bad_tag, evidence, note


async def _confirm_image_pull_bad_tag(issue: IssueReport) -> ConfirmResult:
    confirmed, evidence, _note = await _confirm_image_pull_transient(issue)
    return (not confirmed), evidence, "Bad image tag" if not confirmed else "Transient"


async def _confirm_waiting_reason(issue: IssueReport, reason: str) -> ConfirmResult:
    status = await obs.get_pod_status(issue.pod, issue.namespace)
    actual = None
    for cs in status.container_statuses:
        if cs.waiting and cs.waiting.reason:
            actual = cs.waiting.reason
            break
    confirmed = actual == reason
    return confirmed, {"waiting_reason": actual, "container_statuses": [c.model_dump() for c in status.container_statuses]}, \
        f"waiting.reason={actual}"


async def _confirm_invalid_image_name(issue: IssueReport) -> ConfirmResult:
    return await _confirm_waiting_reason(issue, "InvalidImageName")


async def _confirm_err_image_never_pull(issue: IssueReport) -> ConfirmResult:
    return await _confirm_waiting_reason(issue, "ErrImageNeverPull")


async def _confirm_create_container_error(issue: IssueReport) -> ConfirmResult:
    return await _confirm_waiting_reason(issue, "CreateContainerError")


async def _confirm_config_error_race(issue: IssueReport) -> ConfirmResult:
    c = await obs.diagnose_container_config_error(issue.pod, issue.namespace)
    return c.all_present, c.model_dump(), c.summary


async def _confirm_config_error_missing(issue: IssueReport) -> ConfirmResult:
    c = await obs.diagnose_container_config_error(issue.pod, issue.namespace)
    return (not c.all_present), c.model_dump(), c.summary


_CONTAINER_CREATING_THRESHOLD_S = 300.0


async def _confirm_container_creating_under_threshold(issue: IssueReport) -> ConfirmResult:
    c = await obs.diagnose_stuck_container_creating(issue.pod, issue.namespace)
    confirmed = c.stuck_seconds < _CONTAINER_CREATING_THRESHOLD_S and not c.adverse_events
    return confirmed, c.model_dump(), c.summary


async def _confirm_container_creating_over_threshold(issue: IssueReport) -> ConfirmResult:
    c = await obs.diagnose_stuck_container_creating(issue.pod, issue.namespace)
    confirmed = c.stuck_seconds >= _CONTAINER_CREATING_THRESHOLD_S or bool(c.adverse_events)
    return confirmed, c.model_dump(), c.summary


async def _confirm_pending_insufficient_resources(issue: IssueReport) -> ConfirmResult:
    d = await obs.diagnose_pending(issue.pod, issue.namespace)
    confirmed = any("insufficient" in e.message.lower() for e in d.scheduling_events)
    return confirmed, d.model_dump(), d.summary


async def _confirm_pending_taint_mismatch(issue: IssueReport) -> ConfirmResult:
    d = await obs.diagnose_pending(issue.pod, issue.namespace)
    confirmed = any("taint" in e.message.lower() for e in d.scheduling_events)
    return confirmed, d.model_dump(), d.summary


async def _confirm_pending_unbound_pvc(issue: IssueReport) -> ConfirmResult:
    d = await obs.diagnose_pending(issue.pod, issue.namespace)
    return bool(d.unbound_pvcs), d.model_dump(), d.summary


async def _confirm_node_pressure(issue: IssueReport) -> ConfirmResult:
    c = await obs.check_pod_node_pressure(issue.pod, issue.namespace)
    return c.pressured, c.model_dump(), c.summary


async def _confirm_evicted(issue: IssueReport) -> ConfirmResult:
    status = await obs.get_pod_status(issue.pod, issue.namespace)
    confirmed = status.reason == "Evicted"
    return confirmed, status.model_dump(), f"status.reason={status.reason}"


async def _confirm_volume_mount(issue: IssueReport) -> ConfirmResult:
    events = await obs.get_pod_events(issue.pod, issue.namespace)
    mount_events = [e for e in events if "mount" in e.reason.lower() or "mount" in e.message.lower()]
    status = await obs.get_pod_status(issue.pod, issue.namespace)
    waiting_mount = any(cs.waiting and cs.waiting.reason and "mount" in cs.waiting.reason.lower()
                         for cs in status.container_statuses)
    confirmed = bool(mount_events) or waiting_mount
    return confirmed, {"mount_events": [e.model_dump() for e in mount_events[:5]]}, \
        f"{len(mount_events)} mount-related event(s)"


async def _confirm_endpoint_mismatch(issue: IssueReport) -> ConfirmResult:
    m = await obs.diagnose_endpoint_mismatch(issue.pod, issue.namespace)
    confirmed = m.service is not None and not m.labels_match
    return confirmed, m.model_dump(), m.summary


async def _confirm_rollout_stuck(issue: IssueReport) -> ConfirmResult:
    if not issue.deployment:
        return False, {}, "No deployment resolved — cannot check rollout conditions."
    d = await obs.get_deployment_conditions(issue.deployment, issue.namespace)
    return d.stuck, d.model_dump(), d.summary


async def _confirm_quota_exceeded(issue: IssueReport) -> ConfirmResult:
    q = await obs.get_resourcequota_status(issue.namespace)
    return bool(q.exceeded), q.model_dump(), q.summary


async def _confirm_pdb_blocked(issue: IssueReport) -> ConfirmResult:
    p = await obs.get_pdb_status(issue.namespace)
    return p.blocking, p.model_dump(), p.summary


async def _confirm_hpa_degraded(issue: IssueReport) -> ConfirmResult:
    if not issue.deployment:
        return False, {}, "No deployment resolved — cannot guess HPA name."
    h = await obs.get_hpa_status(issue.deployment, issue.namespace)
    return h.degraded, h.model_dump(), h.summary


async def _confirm_stuck_terminating(issue: IssueReport) -> ConfirmResult:
    status = await obs.get_pod_status(issue.pod, issue.namespace)
    confirmed = status.deletion_timestamp is not None
    return confirmed, status.model_dump(), f"deletion_timestamp={status.deletion_timestamp}"


async def _confirm_probe_transient(issue: IssueReport) -> ConfirmResult:
    status = await obs.get_pod_status(issue.pod, issue.namespace)
    confirmed = status.phase == "Running" and not status.ready
    return confirmed, status.model_dump(), f"phase={status.phase.value}, ready={status.ready}"


async def _confirm_healthy(issue: IssueReport) -> ConfirmResult:
    status = await obs.get_pod_status(issue.pod, issue.namespace)
    confirmed = status.phase == "Succeeded" or (status.phase == "Running" and status.ready)
    return confirmed, status.model_dump(), f"phase={status.phase.value}, ready={status.ready}"


async def _confirm_never(issue: IssueReport) -> ConfirmResult:
    """Used for the Unknown catch-all — never confirms, always falls through to diagnose-only."""
    return False, {}, "No deterministic confirm check exists for this classification."


# ---------------------------------------------------------------------------
# Remediator adapters — thin wrappers so every catalog entry has a uniform
# `async def(issue, dry_run) -> Optional[RemediationResult]` remediator signature,
# and every one of them threads dry_run down into the underlying remediators.py
# call (which all support it natively).
# `None` means "no remediator" (Tier 3 rows, or verify-only rows).
# ---------------------------------------------------------------------------

async def _remediate_patch_memory(issue: IssueReport, dry_run: bool = False) -> Optional[RemediationResult]:
    if not issue.deployment:
        return RemediationResult(
            action="patch_memory_limit", target=issue.pod, namespace=issue.namespace,
            success=False, dry_run=dry_run, message="No deployment resolved — cannot patch memory limit.",
        )
    return await rem.patch_memory_limit(
        deployment=issue.deployment, namespace=issue.namespace, increase_pct=50.0, dry_run=dry_run,
    )


async def _remediate_restart_pod(issue: IssueReport, dry_run: bool = False) -> Optional[RemediationResult]:
    return await rem.restart_pod(pod=issue.pod, namespace=issue.namespace, dry_run=dry_run)


async def _remediate_force_repull(issue: IssueReport, dry_run: bool = False) -> Optional[RemediationResult]:
    if not issue.deployment:
        return RemediationResult(
            action="force_image_repull", target=issue.pod, namespace=issue.namespace,
            success=False, dry_run=dry_run, message="No deployment resolved — cannot trigger rollout restart.",
        )
    return await rem.force_image_repull(deployment=issue.deployment, namespace=issue.namespace, dry_run=dry_run)


async def _remediate_rollback(issue: IssueReport, dry_run: bool = False) -> Optional[RemediationResult]:
    if not issue.deployment:
        return RemediationResult(
            action="rollback_deployment", target=issue.pod, namespace=issue.namespace,
            success=False, dry_run=dry_run, message="No deployment resolved — cannot roll back.",
        )
    return await rem.rollback_deployment(deployment=issue.deployment, namespace=issue.namespace, dry_run=dry_run)


async def _remediate_delete_stuck_pod(issue: IssueReport, dry_run: bool = False) -> Optional[RemediationResult]:
    return await rem.delete_stuck_pod(pod=issue.pod, namespace=issue.namespace, dry_run=dry_run, force=True)


# wait_and_reverify is not a RemediationResult-shaped call — handled specially in dispatch()


# ---------------------------------------------------------------------------
# Action Catalog
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CatalogEntry:
    confirm: Callable[[IssueReport], Awaitable[ConfirmResult]]
    # signature: async def(issue: IssueReport, dry_run: bool = False) -> Optional[RemediationResult]
    remediator: Optional[Callable[..., Awaitable[Optional[RemediationResult]]]]
    tier: ActionTier
    wait_and_reverify: bool = False   # True => use rem.wait_and_reverify instead of `remediator`


ACTION_CATALOG: dict[str, CatalogEntry] = {
    # I. Crash / restart-class
    "OOMKilled":                    CatalogEntry(_confirm_oom_killed, _remediate_patch_memory, ActionTier.AUTO),
    "CrashLoopBackOff":             CatalogEntry(_confirm_crash_transient, _remediate_restart_pod, ActionTier.AUTO),
    "CrashLoopBackOffExit127":      CatalogEntry(_confirm_crash_exit_127, None, ActionTier.DIAGNOSE_ONLY),
    "CrashLoopBackOffExit126":      CatalogEntry(_confirm_crash_exit_126, None, ActionTier.DIAGNOSE_ONLY),
    "InitContainerFailure":         CatalogEntry(_confirm_init_crash_transient, _remediate_restart_pod, ActionTier.AUTO),
    "InitContainerFailureBadExit":  CatalogEntry(_confirm_init_crash_bad_exit, None, ActionTier.DIAGNOSE_ONLY),

    # II. Image-class
    "ImagePullError":               CatalogEntry(_confirm_image_pull_transient, _remediate_force_repull, ActionTier.AUTO),
    "ImagePullBackOffBadTag":       CatalogEntry(_confirm_image_pull_bad_tag, None, ActionTier.DIAGNOSE_ONLY),
    "InvalidImageName":             CatalogEntry(_confirm_invalid_image_name, None, ActionTier.DIAGNOSE_ONLY),
    "ErrImageNeverPull":            CatalogEntry(_confirm_err_image_never_pull, None, ActionTier.DIAGNOSE_ONLY),

    # III. Config/spec-class
    "CreateContainerConfigError":       CatalogEntry(_confirm_config_error_race, _remediate_restart_pod, ActionTier.AUTO),
    "CreateContainerConfigErrorMissing": CatalogEntry(_confirm_config_error_missing, None, ActionTier.DIAGNOSE_ONLY),
    "CreateContainerError":         CatalogEntry(_confirm_create_container_error, None, ActionTier.DIAGNOSE_ONLY),

    # IV. Scheduling/node-class
    "ContainerCreatingStuck":       CatalogEntry(_confirm_container_creating_under_threshold, None, ActionTier.AUTO, wait_and_reverify=True),
    "ContainerCreatingStuckLong":   CatalogEntry(_confirm_container_creating_over_threshold, None, ActionTier.DIAGNOSE_ONLY),
    "FailedScheduling":             CatalogEntry(_confirm_pending_insufficient_resources, None, ActionTier.DIAGNOSE_ONLY),
    "TaintMismatch":                CatalogEntry(_confirm_pending_taint_mismatch, None, ActionTier.DIAGNOSE_ONLY),
    "UnboundPVC":                   CatalogEntry(_confirm_pending_unbound_pvc, None, ActionTier.DIAGNOSE_ONLY),
    "NodePressure":                 CatalogEntry(_confirm_node_pressure, None, ActionTier.DIAGNOSE_ONLY),
    "Evicted":                      CatalogEntry(_confirm_evicted, None, ActionTier.DIAGNOSE_ONLY),
    "VolumeMountError":             CatalogEntry(_confirm_volume_mount, None, ActionTier.DIAGNOSE_ONLY),

    # V. Networking/service-class
    "ProbeFailure":                 CatalogEntry(_confirm_probe_transient, None, ActionTier.AUTO, wait_and_reverify=True),
    "EndpointMismatch":             CatalogEntry(_confirm_endpoint_mismatch, None, ActionTier.DIAGNOSE_ONLY),

    # VI. Controller-level class
    "RolloutStuck":                 CatalogEntry(_confirm_rollout_stuck, _remediate_rollback, ActionTier.AUTO),
    "QuotaExceeded":                CatalogEntry(_confirm_quota_exceeded, None, ActionTier.DIAGNOSE_ONLY),
    "PDBBlocked":                   CatalogEntry(_confirm_pdb_blocked, None, ActionTier.DIAGNOSE_ONLY),
    "HPADegraded":                  CatalogEntry(_confirm_hpa_degraded, None, ActionTier.DIAGNOSE_ONLY),

    # VII. Terminal / catch-all
    "StuckTerminating":             CatalogEntry(_confirm_stuck_terminating, _remediate_delete_stuck_pod, ActionTier.NEEDS_APPROVAL),
    "Healthy":                      CatalogEntry(_confirm_healthy, None, ActionTier.DIAGNOSE_ONLY),
    "Unknown":                      CatalogEntry(_confirm_never, None, ActionTier.DIAGNOSE_ONLY),
}

# Aliases: map common orchestrator CLASSIFICATION spellings onto catalog keys.
_CLASSIFICATION_ALIASES: dict[str, str] = {
    "OOM_KILLED": "OOMKilled",
    "CrashLoop": "CrashLoopBackOff",
    "CrashLoopBackoff": "CrashLoopBackOff",
    "ImagePullBackOff": "ImagePullError",
    "ErrImagePull": "ImagePullError",
    "PendingSchedule": "FailedScheduling",
    "InsufficientResources": "FailedScheduling",
    "InitContainerCrash": "InitContainerFailure",
    "ContainerCreating": "ContainerCreatingStuck",
    "DeploymentRolloutStuck": "RolloutStuck",
    "ProgressDeadlineExceeded": "RolloutStuck",
}


def _normalize_classification(classification: str) -> str:
    """
    Best-effort cleanup for a classification that didn't come back as a bare
    token despite the system prompt asking for one — e.g. the orchestrator
    returning "Healthy (with CPU throttling risk)" instead of "Healthy".
    Strips parenthetical/trailing qualifiers and surrounding whitespace.
    """
    return classification.split("(")[0].split(" - ")[0].strip()


def _lookup_catalog(classification: str) -> tuple[str, CatalogEntry]:
    for candidate in (classification, _normalize_classification(classification)):
        key = _CLASSIFICATION_ALIASES.get(candidate, candidate)
        if key in ACTION_CATALOG:
            return key, ACTION_CATALOG[key]
        # Case-insensitive fallback for near-miss capitalization.
        for catalog_key in ACTION_CATALOG:
            if catalog_key.lower() == key.lower():
                return catalog_key, ACTION_CATALOG[catalog_key]

    return "Unknown", ACTION_CATALOG["Unknown"]


# ---------------------------------------------------------------------------
# Next-steps narrative (small, standalone Claude call — same pattern as
# l1_runbook._generate_l2_actions, re-implemented here so this module has no
# dependency on l1_runbook.py)
# ---------------------------------------------------------------------------

def _fallback_next_steps(classification: str) -> list[str]:
    return [
        "Review `kubectl describe pod` output for the affected pod",
        "Check application logs for the last 24 hours for patterns",
        f"Search runbooks / past incidents for classification '{classification}'",
    ]


async def _generate_next_steps(
    issue: IssueReport,
    tier: ActionTier,
    confirmed: bool,
    evidence: dict,
    action_taken: Optional[RemediationResult],
    api_key: Optional[str],
) -> list[str]:
    try:
        client = anthropic.AsyncAnthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY", ""))
        if not client.api_key:
            return _fallback_next_steps(issue.classification)

        evidence_text = json.dumps(evidence, default=str)[:1500]
        action_text = (
            f"{action_taken.action}: {'ok' if action_taken.success else 'fail'} — {action_taken.message}"
            if action_taken else "none attempted"
        )

        prompt = f"""SRE remediation agent finished acting on a K8s pod issue.

Pod: {issue.pod} | ns: {issue.namespace} | deployment: {issue.deployment or 'none'}
Classification: {issue.classification} | confirmed: {confirmed} | tier: {tier.value}
Root cause: {issue.root_cause or issue.finding}
Action taken: {action_text}
Evidence: {evidence_text}

List 3-5 concrete next steps for a human, kubectl commands where relevant.
Numbered list only, nothing else."""

        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        steps = []
        for line in text.splitlines():
            line = line.strip()
            if line and (line[0].isdigit() or line.startswith("-")):
                clean = line.lstrip("0123456789.-) ").strip()
                if clean:
                    steps.append(clean)
        return steps if steps else _fallback_next_steps(issue.classification)
    except Exception:
        return _fallback_next_steps(issue.classification)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

async def dispatch(
    issue: IssueReport,
    dry_run: bool = False,
    auto_approve: bool = False,
    api_key: Optional[str] = None,
) -> RemediationSummary:
    """
    Fix-stage entry point. Looks up the Action Catalog, independently
    re-validates the classification, and executes/proposes/escalates
    according to the matched tier.
    """
    t0 = time.monotonic()
    catalog_key, entry = _lookup_catalog(issue.classification)

    confirmed, fresh_evidence, confirm_note = await entry.confirm(issue)

    action_taken: Optional[RemediationResult] = None
    verified_healthy: Optional[bool] = None
    resolution: RemediationResolution
    effective_tier = entry.tier

    if not confirmed:
        # Safety backstop: whatever tier the classification implies, an
        # unconfirmed classification is always treated as diagnose-only.
        effective_tier = ActionTier.DIAGNOSE_ONLY
        resolution = RemediationResolution.DIAGNOSE_ONLY

    elif catalog_key == "Healthy":
        resolution = RemediationResolution.HEALTHY

    elif entry.tier == ActionTier.DIAGNOSE_ONLY:
        resolution = RemediationResolution.DIAGNOSE_ONLY

    elif entry.tier == ActionTier.NEEDS_APPROVAL and not auto_approve:
        resolution = RemediationResolution.PROPOSED

    elif entry.tier in (ActionTier.AUTO, ActionTier.NEEDS_APPROVAL):
        if entry.wait_and_reverify:
            if dry_run:
                action_taken = RemediationResult(
                    action="wait_and_reverify", target=issue.pod, namespace=issue.namespace,
                    success=True, dry_run=True,
                    message="Dry-run — would wait then re-check pod readiness (no kubectl write involved).",
                )
                resolution = RemediationResolution.PROPOSED
            else:
                healthy, msg = await rem.wait_and_reverify(pod=issue.pod, namespace=issue.namespace)
                verified_healthy = healthy
                action_taken = RemediationResult(
                    action="wait_and_reverify", target=issue.pod, namespace=issue.namespace,
                    success=healthy, message=msg,
                )
                resolution = RemediationResolution.RESOLVED if healthy else RemediationResolution.ESCALATED
        elif entry.remediator is None:
            resolution = RemediationResolution.DIAGNOSE_ONLY
        else:
            action_taken = await entry.remediator(issue, dry_run=dry_run)
            if dry_run:
                resolution = RemediationResolution.PROPOSED
            elif action_taken and action_taken.success:
                healthy, _msg = await rem.verify_pod_healthy(
                    pod_prefix=issue.pod.rsplit("-", 2)[0], namespace=issue.namespace,
                )
                verified_healthy = healthy
                resolution = RemediationResolution.RESOLVED if healthy else RemediationResolution.ESCALATED
            else:
                resolution = RemediationResolution.ESCALATED
    else:
        resolution = RemediationResolution.DIAGNOSE_ONLY

    merged_evidence = {**issue.evidence, "confirm_check": confirm_note, "fresh_evidence": fresh_evidence}

    next_steps = await _generate_next_steps(
        issue, effective_tier, confirmed, merged_evidence, action_taken, api_key,
    )

    root_cause = issue.root_cause or issue.finding or confirm_note

    return RemediationSummary(
        pod=issue.pod,
        namespace=issue.namespace,
        deployment=issue.deployment,
        classification=catalog_key,
        confidence=issue.confidence,
        tier=effective_tier,
        confirmed=confirmed,
        resolution=resolution,
        action_taken=action_taken,
        verified_healthy=verified_healthy,
        root_cause=root_cause,
        evidence=merged_evidence,
        next_steps=next_steps,
        duration_s=round(time.monotonic() - t0, 2),
        summary=(
            f"{catalog_key} — {'confirmed' if confirmed else 'NOT confirmed (downgraded to diagnose-only)'}, "
            f"tier={effective_tier.value}, resolution={resolution.value}."
        ),
    )
