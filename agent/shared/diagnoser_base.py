"""
Domain-agnostic Claude one-shot diagnosis call.

Extracts the cached-prompt + forced-tool_choice + parse boilerplate that every
diagnoser shares. A new domain provides only a system prompt and a formatted
user_text; the Claude mechanics (prompt caching, forced tool_choice, token
logging, tool_use parsing) come from here.

This module is intentionally K8s-API-free — it must never import a domain client.
"""

import structlog
from anthropic import AsyncAnthropic
from pydantic import BaseModel

from agent.shared.models import DiagnosisResult

logger = structlog.get_logger()


async def diagnose_with_claude(
    client: AsyncAnthropic,
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
    Make a single forced-tool_choice Claude call and return the parsed result.

    The system prompt is wrapped with an ephemeral cache_control block so repeated
    calls in a session hit the prompt cache. The tool's input schema is generated
    from ``result_model`` so any change to the result model (e.g. enum tightening)
    flows through automatically.

    Args:
        client:           Async Anthropic client.
        system_prompt:    Static system prompt (cached across calls).
        user_text:        Formatted user message (the signal bundle, etc.).
        result_model:     Pydantic model used both as the tool schema and parser.
        model:            Anthropic model id.
        max_tokens:       Max output tokens.
        tool_name:        Name of the forced tool.
        tool_description: Description shown to Claude for the forced tool.
        input_schema:     Optional explicit tool input schema. Defaults to
                          ``result_model.model_json_schema()``. A domain whose
                          result model uses an OPEN action_type str (post-D-01)
                          passes an enum-reinjected schema here so its Claude call
                          stays API-constrained to that domain's known actions
                          while the shared model stays open.

    Returns:
        An instance of ``result_model`` parsed from the tool_use block.

    Raises:
        RuntimeError: if Claude did not return the forced tool call.
    """
    response = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=[
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},  # cached across calls
            }
        ],
        messages=[{"role": "user", "content": user_text}],
        tools=[
            {
                "name": tool_name,
                "description": tool_description,
                "input_schema": input_schema
                if input_schema is not None
                else result_model.model_json_schema(),
            }
        ],
        tool_choice={"type": "tool", "name": tool_name},
    )

    cache_info = {
        "input_tokens": response.usage.input_tokens,
        "cache_read": getattr(response.usage, "cache_read_input_tokens", 0),
        "cache_write": getattr(response.usage, "cache_creation_input_tokens", 0),
        "output_tokens": response.usage.output_tokens,
    }
    logger.info("diagnoser_tokens", **cache_info)

    for block in response.content:
        if block.type == "tool_use" and block.name == tool_name:
            result = result_model.model_validate(block.input)
            logger.info(
                "diagnoser_complete",
                confidence=getattr(result, "confidence", None),
                action_type=getattr(result, "action_type", None),
                blast_radius=getattr(result, "estimated_blast_radius", None),
            )
            return result

    raise RuntimeError(f"Claude did not return a {tool_name} tool call")
