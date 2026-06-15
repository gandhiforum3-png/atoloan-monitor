"""
Domain-agnostic Claude one-shot diagnosis call.

Extracts the cached-prompt + forced-tool_choice + parse boilerplate that every
diagnoser shares. A new domain provides only a system prompt and a formatted
user_text; the Claude mechanics (prompt caching, forced tool_choice, token
logging, tool_use parsing) come from here.

This module is intentionally K8s-API-free — it must never import a domain client.
"""

import structlog
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

from agent.shared.models import DiagnosisResult

logger = structlog.get_logger()


async def diagnose_with_claude(
    client: ChatAnthropic,
    system_prompt: str,
    user_text: str,
    *,
    result_model: type[BaseModel] = DiagnosisResult,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 2048,
    tool_name: str = "submit_diagnosis",
    tool_description: str = "Submit the completed RCA diagnosis",
    input_schema: dict | None = None,
) -> BaseModel:
    """
    Make a single forced structured-output Claude call and return the parsed result.

    The system prompt is wrapped with an ephemeral cache_control block so repeated
    calls in a session hit the prompt cache. The tool's input schema is generated
    from ``result_model`` so any change to the result model (e.g. enum tightening)
    flows through automatically.

    Args:
        client:           A ChatAnthropic client, constructed by the caller with
                          ``model``/``max_tokens`` already bound.
        system_prompt:    Static system prompt (cached across calls).
        user_text:        Formatted user message (the signal bundle, etc.).
        result_model:     Pydantic model used both as the tool schema and parser.
        model:            Anthropic model id. Unused here — the caller constructs
                          ``client`` with this already bound; kept for signature
                          compatibility (accept-and-ignore).
        max_tokens:       Max output tokens. Unused here for the same reason as
                          ``model`` above.
        tool_name:        Name of the forced tool. No longer meaningful to
                          ``with_structured_output`` with a bare schema dict;
                          kept for signature compatibility (used only in the
                          RuntimeError message below).
        tool_description: Description shown to Claude for the forced tool.
                          Unused with a bare schema dict; kept for signature
                          compatibility so existing call sites need no change.
        input_schema:     Optional explicit tool input schema. Defaults to
                          ``result_model.model_json_schema()``. A domain whose
                          result model uses an OPEN action_type str (post-D-01)
                          passes an enum-reinjected schema here so its Claude call
                          stays API-constrained to that domain's known actions
                          while the shared model stays open.

    Returns:
        An instance of ``result_model`` parsed from the structured output.

    Raises:
        RuntimeError: if Claude did not return a parseable structured result.
    """
    schema = input_schema if input_schema is not None else result_model.model_json_schema()
    structured = client.with_structured_output(schema, include_raw=True)

    out = await structured.ainvoke([
        SystemMessage(content=[
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},  # cached across calls
            }
        ]),
        HumanMessage(content=user_text),
    ])

    if out["parsing_error"] is not None or out["parsed"] is None:
        raise RuntimeError(f"Claude did not return a {tool_name} tool call")

    result = result_model.model_validate(out["parsed"])

    raw = out["raw"]
    usage = raw.usage_metadata or {}
    input_details = usage.get("input_token_details", {}) or {}
    cache_info = {
        "input_tokens": usage.get("input_tokens", 0),
        "cache_read": input_details.get("cache_read", 0),
        "cache_write": input_details.get("cache_creation", 0),
        "output_tokens": usage.get("output_tokens", 0),
    }
    logger.info("diagnoser_tokens", **cache_info)

    logger.info(
        "diagnoser_complete",
        confidence=getattr(result, "confidence", None),
        action_type=getattr(result, "action_type", None),
        blast_radius=getattr(result, "estimated_blast_radius", None),
    )
    return result
