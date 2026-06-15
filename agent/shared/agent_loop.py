"""
Domain-agnostic agentic tool-use loop.

Extracts the iterate / dispatch / finish_incident / auto-escalate control flow
that every agent shares. A new domain provides only a system prompt, a TOOLS
list, a ``dispatch_tool(name, input, ctx)`` callable, a ``format_bundle`` callable,
and a pre-built ``ctx`` dict; the Claude loop mechanics come from here.

The loop never inspects ``ctx`` contents — it only passes ``ctx`` through to
``dispatch_tool``, so all domain-specific resources (e.g. K8s API clients) stay
out of this module. This module is intentionally K8s-API-free.

Auto-escalation on max-iterations is parameterized by ``domain`` — it is NOT
hardcoded to "k8s", so future domains escalate correctly.
"""

import json
from typing import Callable

import structlog
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from redis.asyncio import Redis

from agent.shared.models import DiagnosisResult, SignalBundle
from agent.shared.remediation import _log_action
from agent.skills.remediators import human_escalator

logger = structlog.get_logger()


async def run_tool_loop(
    anthropic_client: ChatAnthropic,
    redis: Redis,
    bundle: SignalBundle,
    incident_id: str,
    *,
    system_prompt: str,
    tools: list,
    dispatch_tool: Callable,
    format_bundle: Callable,
    ctx: dict,
    domain: str,
    learning_mode: bool,
    max_iterations: int = 6,
    model: str = "claude-sonnet-4-6",
) -> str:
    """
    Run the agentic tool-use loop for one incident and return the outcome string.

    The loop drives Claude through up to ``max_iterations`` rounds: each round it
    sends the running message history + ``tools``, dispatches every tool call
    via ``dispatch_tool(name, input, ctx)``, and feeds results back. It returns the
    ``outcome`` from ``finish_incident`` when Claude calls it. If the budget is
    exhausted without resolution, it auto-escalates to a human (using ``domain``)
    and returns ``"max_iterations_reached"``.

    Args:
        anthropic_client: A ChatAnthropic client, constructed by the caller with
                          ``model`` already bound.
        redis:            Async Redis client (for action logging + escalation).
        bundle:           Signal bundle for this incident.
        incident_id:      Stable incident identifier.
        system_prompt:    Static system prompt (cached across rounds).
        tools:            Anthropic tool definitions for this domain (passed to
                          ``bind_tools``).
        dispatch_tool:    async (name, input, ctx) -> dict result.
        format_bundle:    (bundle) -> str initial user message.
        ctx:              Opaque domain context passed to dispatch_tool. Never
                          inspected by the loop.
        domain:           Domain key ("k8s", "db", ...) for logging + escalation.
        learning_mode:    Recorded for visibility; enforcement lives in the tools.
        max_iterations:   Max tool-use rounds before auto-escalation.
        model:            Anthropic model id. Unused here — the caller constructs
                          ``anthropic_client`` with this already bound; kept for
                          signature compatibility (accept-and-ignore).
    """
    logger.info(
        "agent_loop_started",
        incident_id=incident_id,
        domain=domain,
        signal_count=len(bundle.signals),
        learning_mode=learning_mode,
    )

    bound = anthropic_client.bind_tools(tools)
    system_msg = SystemMessage(content=[
        {
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"},
        }
    ])

    messages: list = [HumanMessage(content=format_bundle(bundle))]

    for iteration in range(1, max_iterations + 1):
        response: AIMessage = await bound.ainvoke([system_msg, *messages])
        messages.append(response)

        finish_input: dict | None = None

        for tc in response.tool_calls:
            name, tool_input, call_id = tc["name"], tc["args"], tc["id"]

            if name == "finish_incident":
                finish_input = tool_input
                messages.append(ToolMessage(content="acknowledged", tool_call_id=call_id))
                continue

            try:
                result = await dispatch_tool(name, tool_input, ctx)
            except Exception as exc:
                logger.error(
                    "agent_tool_error", incident_id=incident_id, tool=name, error=str(exc)
                )
                result = {"error": str(exc)}

            logger.info(
                "agent_tool_call",
                incident_id=incident_id,
                iteration=iteration,
                tool=name,
                tool_input=tool_input,
                result=result,
            )
            messages.append(ToolMessage(content=json.dumps(result), tool_call_id=call_id))

        if finish_input is not None:
            outcome = finish_input.get("outcome", "unknown")
            logger.info(
                "agent_finished",
                incident_id=incident_id,
                iteration=iteration,
                outcome=outcome,
                summary=finish_input.get("summary", ""),
            )
            await _log_action(
                redis, incident_id, domain, "agent_finish", outcome, 0.0,
                finish_input.get("summary", ""),
            )
            return outcome

        if not response.tool_calls:
            # Claude responded with text only and didn't call finish_incident.
            logger.warning("agent_no_tool_call", incident_id=incident_id, iteration=iteration)
            break

    logger.warning("agent_max_iterations", incident_id=incident_id, max_iterations=max_iterations)
    diagnosis = DiagnosisResult(
        root_cause="Agent loop did not reach a resolution within its iteration budget.",
        affected_components=[],
        contributing_factors=[],
        confidence=0.0,
        confidence_reasoning="Agent loop did not converge.",
        recommended_action="Manual investigation required.",
        action_type="human_escalate",
        requires_human_review=True,
        estimated_blast_radius="service",
    )
    await human_escalator.escalate(
        redis,
        domain=domain,
        diagnosis=diagnosis,
        signals=bundle.signals,
        why="agent loop exceeded max iterations without resolution",
    )
    return "max_iterations_reached"
