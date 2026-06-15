"""
RED tests for diagnoser_base.diagnose_with_claude on the ChatAnthropic shape.

These tests mock a ChatAnthropic-like client directly (passed as the `client`
argument) so they exercise the NEW with_structured_output(..., include_raw=True)
contract without needing real langchain_anthropic network calls. They are
written FIRST and are expected to FAIL against the current raw-`anthropic`
implementation of diagnose_with_claude (which calls client.messages.create).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog
from langchain_core.messages import AIMessage, SystemMessage

from agent.shared.diagnoser_base import diagnose_with_claude
from agent.shared.models import DiagnosisResult


def _make_mock_client(parsed: dict | None, parsing_error: Exception | None = None,
                       usage_metadata: dict | None = None):
    """Build a mock ChatAnthropic-like client whose with_structured_output(...)
    returns a mock runnable whose ainvoke returns the include_raw=True dict shape."""
    if usage_metadata is None:
        usage_metadata = {
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "input_token_details": {"cache_read": 50, "cache_creation": 10},
        }

    raw_msg = AIMessage(content="", usage_metadata=usage_metadata)

    structured_runnable = MagicMock()
    structured_runnable.ainvoke = AsyncMock(return_value={
        "raw": raw_msg,
        "parsed": parsed,
        "parsing_error": parsing_error,
    })

    client = MagicMock()
    client.with_structured_output = MagicMock(return_value=structured_runnable)
    return client, structured_runnable


def _valid_diagnosis_dict(**overrides) -> dict:
    defaults = dict(
        root_cause="node memory pressure",
        affected_components=["ns/dep"],
        contributing_factors=[],
        confidence=0.9,
        confidence_reasoning="strong evidence",
        recommended_action="restart pod",
        action_type="pod_restart",
        requires_human_review=False,
        estimated_blast_radius="service",
    )
    defaults.update(overrides)
    return defaults


@pytest.mark.asyncio
async def test_diagnose_with_claude_returns_parsed_result():
    parsed = _valid_diagnosis_dict()
    client, structured_runnable = _make_mock_client(parsed=parsed)

    result = await diagnose_with_claude(
        client,
        "system prompt text",
        "user text",
    )

    assert isinstance(result, DiagnosisResult)
    assert result.action_type == "pod_restart"
    assert result.confidence == 0.9

    # with_structured_output called with the default result_model schema, include_raw=True
    client.with_structured_output.assert_called_once()
    args, kwargs = client.with_structured_output.call_args
    assert args[0] == DiagnosisResult.model_json_schema()
    assert kwargs.get("include_raw") is True


@pytest.mark.asyncio
async def test_diagnose_with_claude_preserves_cache_control_on_system_prompt():
    parsed = _valid_diagnosis_dict()
    client, structured_runnable = _make_mock_client(parsed=parsed)

    await diagnose_with_claude(
        client,
        "my cached system prompt",
        "user text",
    )

    structured_runnable.ainvoke.assert_awaited_once()
    (messages,), _ = structured_runnable.ainvoke.call_args
    system_msg = messages[0]
    assert isinstance(system_msg, SystemMessage)
    assert system_msg.content == [
        {
            "type": "text",
            "text": "my cached system prompt",
            "cache_control": {"type": "ephemeral"},
        }
    ]


@pytest.mark.asyncio
async def test_diagnose_with_claude_uses_explicit_input_schema_override():
    parsed = _valid_diagnosis_dict()
    client, structured_runnable = _make_mock_client(parsed=parsed)

    custom_schema = DiagnosisResult.model_json_schema()
    custom_schema["properties"]["action_type"]["enum"] = [
        "pod_restart", "deployment_scale_down", "human_escalate", "observe_only",
    ]

    await diagnose_with_claude(
        client,
        "system prompt",
        "user text",
        input_schema=custom_schema,
    )

    args, kwargs = client.with_structured_output.call_args
    assert args[0] == custom_schema
    assert kwargs.get("include_raw") is True


@pytest.mark.asyncio
async def test_diagnose_with_claude_raises_on_parsing_error():
    client, structured_runnable = _make_mock_client(
        parsed=None, parsing_error=ValueError("bad tool call"),
    )

    with pytest.raises(RuntimeError):
        await diagnose_with_claude(
            client,
            "system prompt",
            "user text",
        )


@pytest.mark.asyncio
async def test_diagnose_with_claude_logs_token_and_result_fields():
    parsed = _valid_diagnosis_dict(
        action_type="pod_restart", confidence=0.9, estimated_blast_radius="service",
    )
    client, structured_runnable = _make_mock_client(parsed=parsed)

    with structlog.testing.capture_logs() as captured:
        await diagnose_with_claude(
            client,
            "system prompt",
            "user text",
        )

    token_events = [e for e in captured if e.get("event") == "diagnoser_tokens"]
    assert len(token_events) == 1
    token_event = token_events[0]
    assert token_event["cache_read"] == 50
    assert token_event["cache_write"] == 10
    assert token_event["input_tokens"] == 100
    assert token_event["output_tokens"] == 20

    complete_events = [e for e in captured if e.get("event") == "diagnoser_complete"]
    assert len(complete_events) == 1
    complete_event = complete_events[0]
    assert complete_event["action_type"] == "pod_restart"
    assert complete_event["confidence"] == 0.9
    assert complete_event["blast_radius"] == "service"
