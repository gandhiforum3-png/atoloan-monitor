"""
remediate.py — Standalone Find -> Fix -> Summarize CLI.

Pipeline:
    FIND      — orchestrator.find_issue() investigates the pod (LLM + read-only tools)
    FIX       — remediation_agent.dispatch() independently re-validates the
                classification and acts within its tiered Action Catalog
                (see REMEDIATION_AGENT_DESIGN.md)
    SUMMARIZE — prints one structured report of what was found and what was done

Deliberately has no dependency on l1_runbook.py / watcher.py — this is a
separate, on-demand tool: give it a pod, it finds the problem, fixes what it
safely can, and tells you exactly what it did.

Usage:
    python remediate.py <pod> [namespace] [--dry-run] [--auto-approve] [--issue "..."]

Exit codes:
    0 — resolved or already healthy
    1 — escalated, proposed (needs approval), or diagnose-only
    2 — error (bad input, missing API key, Find/Fix stage failure)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from models import IssueReport, RemediationResolution, RemediationSummary
from orchestrator import find_issue
from remediation_agent import dispatch


_RESOLUTION_ICON = {
    RemediationResolution.RESOLVED:      "✓",   # check
    RemediationResolution.HEALTHY:       "✓",
    RemediationResolution.ESCALATED:     "↑",   # up arrow
    RemediationResolution.PROPOSED:      "?",
    RemediationResolution.DIAGNOSE_ONLY: "↑",
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find, fix, and summarize a Kubernetes pod issue.",
    )
    parser.add_argument("pod", help="Pod name to investigate")
    parser.add_argument(
        "namespace", nargs="?", default="default",
        help="Kubernetes namespace (default: default)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run the full pipeline but never execute a kubectl write",
    )
    parser.add_argument(
        "--auto-approve", action="store_true",
        help="Also execute Tier-2 (needs-approval) actions, e.g. force-deleting a stuck-Terminating pod",
    )
    parser.add_argument(
        "--issue", default="",
        help="Optional human description of the symptom, passed to the Find stage",
    )
    return parser.parse_args()


def _print_summary(issue: IssueReport, summary: RemediationSummary) -> None:
    print(f"\n{'=' * 70}")
    print(f"POD: {summary.pod}  NAMESPACE: {summary.namespace}  DEPLOYMENT: {summary.deployment or 'none'}")
    print(f"{'=' * 70}")

    print("\nFOUND")
    print(f"  Classification : {summary.classification}  (confidence: {summary.confidence})")
    print(f"  Confirmed      : {summary.confirmed}")
    print(f"  Root cause     : {summary.root_cause or issue.finding}")

    print("\nDID")
    if summary.action_taken:
        icon = "✓" if summary.action_taken.success else "✗"
        dry = " [dry-run]" if summary.action_taken.dry_run else ""
        print(f"  {icon} {summary.action_taken.action}{dry}: {summary.action_taken.message}")
    else:
        print(f"  (no action executed — tier={summary.tier.value})")

    print("\nRESULT")
    icon = _RESOLUTION_ICON.get(summary.resolution, "·")
    print(f"  {icon} {summary.resolution.value.upper()}  ({summary.duration_s}s)")
    if summary.verified_healthy is not None:
        print(f"  verified_healthy: {summary.verified_healthy}")
    print(f"  {summary.summary}")

    print("\nNEXT STEPS")
    for step in summary.next_steps:
        print(f"  - {step}")
    print()


def _exit_code(resolution: RemediationResolution) -> int:
    if resolution in (RemediationResolution.RESOLVED, RemediationResolution.HEALTHY):
        return 0
    return 1


async def _main() -> int:
    args = _parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("[error] ANTHROPIC_API_KEY is not set — the Find stage requires it.", file=sys.stderr)
        return 2

    print(f"[FIND] Investigating pod '{args.pod}' in namespace '{args.namespace}'...")
    try:
        issue = await find_issue(args.pod, args.namespace, issue_description=args.issue)
    except Exception as exc:
        print(f"[error] Find stage failed: {exc}", file=sys.stderr)
        return 2

    print(
        f"[FIND] classification={issue.classification} confidence={issue.confidence} "
        f"deployment={issue.deployment or 'none'}"
    )

    print(f"[FIX] {'(dry-run) ' if args.dry_run else ''}evaluating Action Catalog...")
    try:
        summary = await dispatch(issue, dry_run=args.dry_run, auto_approve=args.auto_approve)
    except Exception as exc:
        print(f"[error] Fix stage failed: {exc}", file=sys.stderr)
        return 2

    _print_summary(issue, summary)
    return _exit_code(summary.resolution)


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
