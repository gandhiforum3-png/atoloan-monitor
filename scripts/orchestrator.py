"""
orchestrator.py — Agentic loop that lets Claude drive k8s pod observation autonomously.

Claude receives the pod observer tools, decides which to call, interprets the results,
and iterates until it has a complete diagnosis with recommended actions.

Two entry points:
  diagnose_pod()         — investigate a specific pod
  triage_namespace()     — sweep a namespace and explain what is wrong

Both return a DiagnosisResult with findings, evidence, root cause, and next steps.

Usage:
    import asyncio
    from orchestrator import diagnose_pod, triage_namespace

    result = asyncio.run(diagnose_pod("api-6b9df7-xzp2k", namespace="prod"))
    print(result.summary)
    print(result.next_steps)
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Optional

import anthropic

from k8s_tools import POD_OBSERVER_TOOLS, execute_tool, tool_result_block
from models import IssueReport
from pod_observer import resolve_owning_deployment


# ---------------------------------------------------------------------------
# Result schema
# ---------------------------------------------------------------------------

@dataclass
class DiagnosisResult:
    """Structured output from the orchestrator."""
    pod:        Optional[str]  = None
    namespace:  str            = "default"
    # Claude's final structured answer
    finding:    str            = ""     # one-sentence bottom line
    evidence:   str            = ""     # specific data that supports it
    root_cause: str            = ""     # why it's happening
    next_steps: list[str]      = field(default_factory=list)
    # Full narrative (raw final text from Claude)
    summary:    str            = ""
    # How many tool calls Claude made
    tool_calls_made: int       = 0


# ---------------------------------------------------------------------------
# Shared agent loop
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are an SRE expert specialising in Kubernetes pod health.
You have access to a set of pod observer tools. Your job is to investigate
the reported problem and produce a clear, actionable diagnosis.

Follow this discipline:
1. Gather facts first — run the tools, do not guess.
2. Run composite tools (diagnose_crash, diagnose_pending, namespace_health_sweep)
   first for efficiency; drill into specific tools only if you need more detail.
3. Interpret in order: phase → container state → events → logs → resources.
4. Actively check for failure modes beyond the obvious five (OOMKilled,
   CrashLoopBackOff, ImagePullBackOff, FailedScheduling, ProbeFailure) — use the
   more specific tools when the signature suggests them:
     - CreateContainerConfigError / a missing config reference -> diagnose_container_config_error
     - An init container crash-looping -> diagnose_init_container_crash
     - A pod stuck ContainerCreating -> diagnose_stuck_container_creating
     - A Running pod whose node might be pressured -> check_pod_node_pressure
     - A healthy pod that seems to receive no traffic -> diagnose_endpoint_mismatch
     - A Deployment that never finished rolling out -> get_deployment_conditions
     - A namespace-level quota, PDB, or HPA problem -> get_resourcequota_status,
       get_pdb_status, get_hpa_status
   Call resolve_owning_deployment early — most remediation actions need it.
5. Stop calling tools once you have enough evidence to state the root cause confidently.

Always end with a structured answer in this exact format:

FINDING: <one sentence — what is wrong>
EVIDENCE: <the specific data that proves it>
ROOT CAUSE: <why it is happening>
CLASSIFICATION: <exactly one token from this list, nothing else on the line —
  no parentheses, no qualifiers, no extra words, even if you have caveats:
  OOMKilled, CrashLoopBackOff, ImagePullBackOff, CreateContainerConfigError,
  CreateContainerError, InvalidImageName, ErrImageNeverPull, ContainerCreatingStuck,
  InitContainerFailure, FailedScheduling, NodePressure, EndpointMismatch,
  VolumeMountError, Evicted, RolloutStuck, QuotaExceeded, PDBBlocked, TaintMismatch,
  HPADegraded, Healthy, Unknown
  Put any caveat, risk, or nuance in ROOT CAUSE or NEXT STEPS instead — e.g. a pod
  that is Running+Ready but at risk of CPU throttling is still CLASSIFICATION: Healthy,
  with the throttling risk described in ROOT CAUSE and a mitigation in NEXT STEPS.>
CONFIDENCE: <high|medium|low — how directly the evidence supports CLASSIFICATION>
DEPLOYMENT: <owning deployment name, or "none">
NEXT STEPS:
- <action 1>
- <action 2>
"""


async def _run_agent_loop(
    initial_message: str,
    client: anthropic.AsyncAnthropic,
    model: str = "claude-sonnet-4-6",
    max_iterations: int = 8,
) -> tuple[str, int, list[dict]]:
    """
    Core agentic tool-use loop.
    Returns (final_text, tool_calls_made, tool_trace).
    tool_trace is every {tool, input, result} call made during the loop —
    the raw evidence behind the final narrative, used by find_issue() to
    populate IssueReport.evidence for the Remediation Agent.
    """
    messages = [{"role": "user", "content": initial_message}]
    tool_calls_made = 0
    tool_trace: list[dict] = []

    for _ in range(max_iterations):
        response = await client.messages.create(
            model=model,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=POD_OBSERVER_TOOLS,
            messages=messages,
        )

        # Append assistant turn
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            # Extract final text
            final_text = " ".join(
                block.text for block in response.content
                if hasattr(block, "text")
            )
            return final_text, tool_calls_made, tool_trace

        if response.stop_reason == "tool_use":
            # Execute all tool calls in this turn in parallel
            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
            tool_calls_made += len(tool_use_blocks)

            results = await asyncio.gather(*[
                execute_tool(b.name, b.input)
                for b in tool_use_blocks
            ])

            for block, result in zip(tool_use_blocks, results):
                tool_trace.append({"tool": block.name, "input": block.input, "result": result})

            tool_result_blocks = [
                tool_result_block(block.id, result)
                for block, result in zip(tool_use_blocks, results)
            ]

            messages.append({"role": "user", "content": tool_result_blocks})

        else:
            # Unexpected stop reason — bail out
            break

    return "Max iterations reached without a final diagnosis.", tool_calls_made, tool_trace


def _parse_structured_answer(text: str) -> tuple[str, str, str, list[str]]:
    """Parse the FINDING / EVIDENCE / ROOT CAUSE / NEXT STEPS block from Claude's response."""
    finding     = ""
    evidence    = ""
    root_cause  = ""
    next_steps: list[str] = []

    current = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("FINDING:"):
            finding = line[len("FINDING:"):].strip()
            current = "finding"
        elif line.startswith("EVIDENCE:"):
            evidence = line[len("EVIDENCE:"):].strip()
            current = "evidence"
        elif line.startswith("ROOT CAUSE:"):
            root_cause = line[len("ROOT CAUSE:"):].strip()
            current = "root_cause"
        elif line.startswith("NEXT STEPS:"):
            current = "next_steps"
        elif current == "next_steps" and line.startswith("-"):
            next_steps.append(line[1:].strip())
        elif current == "evidence" and not finding and line:
            evidence += " " + line
        elif current == "root_cause" and not next_steps and line:
            root_cause += " " + line

    return finding, evidence, root_cause, next_steps


def _parse_issue_fields(text: str) -> tuple[str, str, str]:
    """Parse the CLASSIFICATION / CONFIDENCE / DEPLOYMENT lines the system prompt requires."""
    classification = "Unknown"
    confidence     = "low"
    deployment     = "none"

    for line in text.splitlines():
        line = line.strip()
        if line.startswith("CLASSIFICATION:"):
            classification = line[len("CLASSIFICATION:"):].strip() or "Unknown"
        elif line.startswith("CONFIDENCE:"):
            confidence = line[len("CONFIDENCE:"):].strip().lower() or "low"
        elif line.startswith("DEPLOYMENT:"):
            deployment = line[len("DEPLOYMENT:"):].strip() or "none"

    return classification, confidence, deployment


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

async def diagnose_pod(
    pod: str,
    namespace: str = "default",
    issue_description: str = "",
    model: str = "claude-sonnet-4-6",
    api_key: Optional[str] = None,
) -> DiagnosisResult:
    """
    Let Claude autonomously diagnose a specific pod.

    Args:
        pod:               Pod name to investigate
        namespace:         Kubernetes namespace
        issue_description: Optional human description of the symptom
                           e.g. "pod keeps restarting" or "probe failing"
        model:             Claude model to use
        api_key:           Anthropic API key (falls back to ANTHROPIC_API_KEY env var)

    Returns:
        DiagnosisResult with finding, evidence, root_cause, and next_steps
    """
    client = anthropic.AsyncAnthropic(api_key=api_key or os.environ["ANTHROPIC_API_KEY"])

    issue_context = f" The reported symptom: {issue_description}." if issue_description else ""
    message = (
        f"Investigate pod '{pod}' in namespace '{namespace}'.{issue_context} "
        f"Use the available tools to determine what is wrong and why."
    )

    summary, tool_calls, _trace = await _run_agent_loop(message, client, model)
    finding, evidence, root_cause, next_steps = _parse_structured_answer(summary)

    return DiagnosisResult(
        pod=pod,
        namespace=namespace,
        finding=finding or summary[:200],
        evidence=evidence,
        root_cause=root_cause,
        next_steps=next_steps,
        summary=summary,
        tool_calls_made=tool_calls,
    )


async def triage_namespace(
    namespace: str = "default",
    model: str = "claude-sonnet-4-6",
    api_key: Optional[str] = None,
) -> DiagnosisResult:
    """
    Let Claude sweep a namespace and explain everything that is unhealthy.

    Args:
        namespace: Kubernetes namespace to sweep
        model:     Claude model to use
        api_key:   Anthropic API key (falls back to ANTHROPIC_API_KEY env var)

    Returns:
        DiagnosisResult describing all active issues and recommended next steps
    """
    client = anthropic.AsyncAnthropic(api_key=api_key or os.environ["ANTHROPIC_API_KEY"])

    message = (
        f"Perform a full health triage of namespace '{namespace}'. "
        f"Find all pods that are not healthy, explain why, and prioritise the issues."
    )

    summary, tool_calls, _trace = await _run_agent_loop(message, client, model)
    finding, evidence, root_cause, next_steps = _parse_structured_answer(summary)

    return DiagnosisResult(
        namespace=namespace,
        finding=finding or summary[:200],
        evidence=evidence,
        root_cause=root_cause,
        next_steps=next_steps,
        summary=summary,
        tool_calls_made=tool_calls,
    )


async def find_issue(
    pod: str,
    namespace: str = "default",
    issue_description: str = "",
    model: str = "claude-sonnet-4-6",
    api_key: Optional[str] = None,
) -> IssueReport:
    """
    Find-stage entry point for the standalone remediation pipeline (remediate.py).

    Unlike diagnose_pod(), this:
      - resolves the pod's owning Deployment up front (most remediators need it)
      - requires the agent to emit a machine-readable CLASSIFICATION/CONFIDENCE
      - carries the raw tool-call trace in .evidence, not just prose

    The Remediation Agent does not trust CLASSIFICATION/CONFIDENCE blindly — it
    re-derives the evidence deterministically before acting. This function's job
    is to propose, not decide.

    Returns:
        IssueReport for remediation_agent.dispatch()
    """
    client = anthropic.AsyncAnthropic(api_key=api_key or os.environ["ANTHROPIC_API_KEY"])

    deployment = await resolve_owning_deployment(pod, namespace)

    issue_context = f" The reported symptom: {issue_description}." if issue_description else ""
    deployment_context = (
        f" Owning Deployment: {deployment}." if deployment
        else " This pod has no owning Deployment (bare pod, StatefulSet, or DaemonSet)."
    )
    message = (
        f"Investigate pod '{pod}' in namespace '{namespace}'.{issue_context}{deployment_context} "
        f"Use the available tools to determine what is wrong and why."
    )

    summary, tool_calls, trace = await _run_agent_loop(message, client, model)
    finding, evidence_text, root_cause, next_steps = _parse_structured_answer(summary)
    classification, confidence, parsed_deployment = _parse_issue_fields(summary)

    if not deployment and parsed_deployment not in ("none", ""):
        deployment = parsed_deployment

    return IssueReport(
        pod=pod,
        namespace=namespace,
        deployment=deployment,
        classification=classification,
        confidence=confidence,
        finding=finding or summary[:200],
        root_cause=root_cause,
        evidence={
            "evidence_text": evidence_text,
            "next_steps": next_steps,
            "tool_trace": trace,
        },
        summary=summary,
        tool_calls_made=tool_calls,
    )


# ---------------------------------------------------------------------------
# Streaming variant — yields progress lines as Claude works
# ---------------------------------------------------------------------------

async def diagnose_pod_streaming(
    pod: str,
    namespace: str = "default",
    issue_description: str = "",
    model: str = "claude-sonnet-4-6",
    api_key: Optional[str] = None,
):
    """
    Streaming version of diagnose_pod — yields progress strings as Claude calls tools.
    Useful for feeding into a FastAPI SSE endpoint or a rich console.

    Usage:
        async for line in diagnose_pod_streaming("api-pod", "prod"):
            print(line)
    """
    client = anthropic.AsyncAnthropic(api_key=api_key or os.environ["ANTHROPIC_API_KEY"])

    issue_context = f" Symptom: {issue_description}." if issue_description else ""
    messages = [
        {
            "role": "user",
            "content": (
                f"Investigate pod '{pod}' in namespace '{namespace}'.{issue_context} "
                f"Use the available tools to determine what is wrong and why."
            ),
        }
    ]
    tool_calls_made = 0

    for iteration in range(8):
        response = await client.messages.create(
            model=model,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=POD_OBSERVER_TOOLS,
            messages=messages,
        )

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            final_text = " ".join(
                b.text for b in response.content if hasattr(b, "text")
            )
            yield f"[done] {final_text}"
            return

        if response.stop_reason == "tool_use":
            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
            tool_calls_made += len(tool_use_blocks)

            for b in tool_use_blocks:
                yield f"[tool] {b.name}({json.dumps(b.input)})"

            results = await asyncio.gather(*[
                execute_tool(b.name, b.input)
                for b in tool_use_blocks
            ])

            for b, r in zip(tool_use_blocks, results):
                yield f"[result] {b.name} → {json.dumps(r, default=str)[:300]}…"

            messages.append({
                "role": "user",
                "content": [
                    tool_result_block(b.id, r)
                    for b, r in zip(tool_use_blocks, results)
                ],
            })

    yield "[error] Max iterations reached without a final diagnosis."


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    async def _main():
        if len(sys.argv) < 2:
            print("Usage:")
            print("  python orchestrator.py <pod> [namespace] [issue description]")
            print("  python orchestrator.py --sweep [namespace]")
            sys.exit(1)

        if sys.argv[1] == "--sweep":
            ns = sys.argv[2] if len(sys.argv) > 2 else "default"
            print(f"[*] Triaging namespace '{ns}' …\n")
            result = await triage_namespace(ns)
        else:
            pod = sys.argv[1]
            ns  = sys.argv[2] if len(sys.argv) > 2 else "default"
            issue = " ".join(sys.argv[3:]) if len(sys.argv) > 3 else ""
            print(f"[*] Diagnosing pod '{pod}' in '{ns}' …\n")
            result = await diagnose_pod(pod, ns, issue)

        print(f"FINDING:    {result.finding}")
        print(f"EVIDENCE:   {result.evidence}")
        print(f"ROOT CAUSE: {result.root_cause}")
        print("NEXT STEPS:")
        for step in result.next_steps:
            print(f"  - {step}")
        print(f"\n[tool calls made: {result.tool_calls_made}]")

    asyncio.run(_main())
