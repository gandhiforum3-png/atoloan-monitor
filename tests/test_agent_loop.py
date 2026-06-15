"""
RED tests for agent_loop.run_tool_loop on the ChatAnthropic shape.

These tests mock a ChatAnthropic-like client directly (passed as
``anthropic_client``) so they exercise the NEW bind_tools(...) +
AIMessage.tool_calls / ToolMessage contract without needing real
langchain_anthropic network calls. They are written FIRST and are expected to
FAIL against the current raw-`anthropic` implementation of run_tool_loop
(which calls client.messages.create and inspects response.content blocks).
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage

from agent.shared.agent_loop import run_tool_loop
from agent.shared.models import InfraEvent, SignalBundle


def _make_bundle(make_event) -> SignalBundle:
    return SignalBundle(
        domain="k8s",
        signals=[make_event()],
        started_at=datetime.now(timezone.utc),
        window_seconds=30,
    )


def _make_mock_client(responses: list[AIMessage]):
    """Build a mock ChatAnthropic-like client whose bind_tools(...) returns a
    mock runnable whose ainvoke returns successive AIMessage responses."""
    bound_runnable = MagicMock()
    bound_runnable.ainvoke = AsyncMock(side_effect=responses)

    client = MagicMock()
    client.bind_tools = MagicMock(return_value=bound_runnable)
    return client, bound_runnable


@pytest.fixture
def redis_mock():
    redis = MagicMock()
    redis.xadd = AsyncMock()
    return redis


async def test_run_tool_loop_finish_incident_resolved(make_event, redis_mock):
    bundle = _make_bundle(make_event)

    response = AIMessage(
        content="",
        tool_calls=[{
            "name": "finish_incident",
            "args": {"outcome": "resolved", "summary": "done"},
            "id": "call_1",
            "type": "tool_call",
        }],
    )
    client, bound_runnable = _make_mock_client([response])

    dispatch_tool = AsyncMock()

    with patch("agent.shared.agent_loop._log_action", new=AsyncMock()) as mock_log_action:
        outcome = await run_tool_loop(
            client,
            redis_mock,
            bundle,
            "incident-1",
            system_prompt="system prompt",
            tools=[],
            dispatch_tool=dispatch_tool,
            format_bundle=lambda b: "user text",
            ctx={},
            domain="k8s",
            learning_mode=True,
        )

    assert outcome == "resolved"
    client.bind_tools.assert_called_once_with([])
    bound_runnable.ainvoke.assert_awaited_once()
    mock_log_action.assert_awaited_once()
    _, kwargs = mock_log_action.call_args
    # _log_action positional signature: (redis, incident_id, domain, action, status/outcome, confidence, detail)
    args, kwargs = mock_log_action.call_args
    assert "resolved" in args


async def test_run_tool_loop_dispatches_non_finish_tool_call(make_event, redis_mock):
    bundle = _make_bundle(make_event)

    first_response = AIMessage(
        content="",
        tool_calls=[{
            "name": "some_tool",
            "args": {"x": 1},
            "id": "call_2",
            "type": "tool_call",
        }],
    )
    finish_response = AIMessage(
        content="",
        tool_calls=[{
            "name": "finish_incident",
            "args": {"outcome": "resolved", "summary": "done"},
            "id": "call_3",
            "type": "tool_call",
        }],
    )
    client, bound_runnable = _make_mock_client([first_response, finish_response])

    dispatch_tool = AsyncMock(return_value={"status": "ok"})

    with patch("agent.shared.agent_loop._log_action", new=AsyncMock()):
        outcome = await run_tool_loop(
            client,
            redis_mock,
            bundle,
            "incident-2",
            system_prompt="system prompt",
            tools=[{"name": "some_tool"}],
            dispatch_tool=dispatch_tool,
            format_bundle=lambda b: "user text",
            ctx={"marker": True},
            domain="k8s",
            learning_mode=True,
        )

    assert outcome == "resolved"
    dispatch_tool.assert_awaited_once_with("some_tool", {"x": 1}, {"marker": True})

    # Second ainvoke call's messages must include a ToolMessage for call_2
    assert bound_runnable.ainvoke.await_count == 2
    second_call_messages = bound_runnable.ainvoke.call_args_list[1][0][0]
    tool_messages = [m for m in second_call_messages if isinstance(m, ToolMessage)]
    assert any(tm.tool_call_id == "call_2" for tm in tool_messages)


async def test_run_tool_loop_max_iterations_escalates(make_event, redis_mock):
    bundle = _make_bundle(make_event)

    # Every iteration returns text-only response with no tool calls.
    text_only_response = AIMessage(content="thinking...", tool_calls=[])

    max_iterations = 3
    responses = [text_only_response] * max_iterations
    client, bound_runnable = _make_mock_client(responses)

    dispatch_tool = AsyncMock()

    with patch("agent.shared.agent_loop._log_action", new=AsyncMock()), \
         patch("agent.shared.agent_loop.human_escalator.escalate", new=AsyncMock()) as mock_escalate:
        outcome = await run_tool_loop(
            client,
            redis_mock,
            bundle,
            "incident-3",
            system_prompt="system prompt",
            tools=[],
            dispatch_tool=dispatch_tool,
            format_bundle=lambda b: "user text",
            ctx={},
            domain="k8s",
            learning_mode=True,
            max_iterations=max_iterations,
        )

    assert outcome == "max_iterations_reached"
    mock_escalate.assert_awaited_once()
    _, kwargs = mock_escalate.call_args
    diagnosis = kwargs["diagnosis"]
    assert diagnosis.action_type == "human_escalate"


async def test_run_tool_loop_system_message_carries_cache_control(make_event, redis_mock):
    bundle = _make_bundle(make_event)

    response = AIMessage(
        content="",
        tool_calls=[{
            "name": "finish_incident",
            "args": {"outcome": "resolved", "summary": "done"},
            "id": "call_1",
            "type": "tool_call",
        }],
    )
    client, bound_runnable = _make_mock_client([response])

    dispatch_tool = AsyncMock()

    with patch("agent.shared.agent_loop._log_action", new=AsyncMock()):
        await run_tool_loop(
            client,
            redis_mock,
            bundle,
            "incident-4",
            system_prompt="cached system prompt",
            tools=[],
            dispatch_tool=dispatch_tool,
            format_bundle=lambda b: "user text",
            ctx={},
            domain="k8s",
            learning_mode=True,
        )

    first_call_messages = bound_runnable.ainvoke.call_args_list[0][0][0]
    system_msg = first_call_messages[0]
    assert isinstance(system_msg, SystemMessage)
    assert system_msg.content == [
        {
            "type": "text",
            "text": "cached system prompt",
            "cache_control": {"type": "ephemeral"},
        }
    ]


async def test_run_tool_loop_threads_learning_mode_without_raising(make_event, redis_mock):
    bundle = _make_bundle(make_event)

    response = AIMessage(
        content="",
        tool_calls=[{
            "name": "finish_incident",
            "args": {"outcome": "resolved", "summary": "done"},
            "id": "call_1",
            "type": "tool_call",
        }],
    )
    client, bound_runnable = _make_mock_client([response])

    dispatch_tool = AsyncMock()
    ctx: dict = {}

    with patch("agent.shared.agent_loop._log_action", new=AsyncMock()):
        outcome = await run_tool_loop(
            client,
            redis_mock,
            bundle,
            "incident-5",
            system_prompt="system prompt",
            tools=[],
            dispatch_tool=dispatch_tool,
            format_bundle=lambda b: "user text",
            ctx=ctx,
            domain="k8s",
            learning_mode=False,
        )

    assert outcome == "resolved"
    # The loop itself doesn't gate on learning_mode or mutate ctx with it.
    assert "learning_mode" not in ctx
