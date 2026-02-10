"""Tests for UiPathChatRuntime with mocked runtime and chat bridge."""

from __future__ import annotations

from typing import Any, AsyncGenerator, Sequence, cast
from unittest.mock import AsyncMock, Mock

import pytest
from uipath.core.chat import (
    UiPathConversationMessageEvent,
    UiPathConversationMessageStartEvent,
)

from uipath.runtime import (
    UiPathExecuteOptions,
    UiPathResumeTrigger,
    UiPathResumeTriggerType,
    UiPathRuntimeResult,
    UiPathRuntimeStatus,
    UiPathStreamOptions,
)
from uipath.runtime.chat import (
    UiPathChatProtocol,
    UiPathChatRuntime,
)
from uipath.runtime.events import UiPathRuntimeEvent, UiPathRuntimeMessageEvent
from uipath.runtime.schema import UiPathRuntimeSchema


def make_chat_bridge_mock() -> UiPathChatProtocol:
    """Create a chat bridge mock with all methods that UiPathChatRuntime uses."""
    bridge_mock: Mock = Mock(spec=UiPathChatProtocol)

    bridge_mock.connect = AsyncMock()
    bridge_mock.disconnect = AsyncMock()
    bridge_mock.emit_message_event = AsyncMock()
    bridge_mock.emit_interrupt_event = AsyncMock()
    bridge_mock.wait_for_resume = AsyncMock()

    return cast(UiPathChatProtocol, bridge_mock)


class StreamingMockRuntime:
    """Mock runtime that streams message events and a final result."""

    def __init__(
        self,
        messages: Sequence[str],
        *,
        error_in_stream: bool = False,
    ) -> None:
        super().__init__()
        self.messages: list[str] = list(messages)
        self.error_in_stream: bool = error_in_stream
        self.execute_called: bool = False

    async def dispose(self) -> None:
        pass

    async def execute(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathExecuteOptions | None = None,
    ) -> UiPathRuntimeResult:
        """Fallback execute path."""
        self.execute_called = True
        return UiPathRuntimeResult(
            status=UiPathRuntimeStatus.SUCCESSFUL,
            output={"mode": "execute"},
        )

    async def stream(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathStreamOptions | None = None,
    ) -> AsyncGenerator[UiPathRuntimeEvent, None]:
        """Async generator yielding message events and final result."""
        if self.error_in_stream:
            raise RuntimeError("Stream blew up")

        for idx, _message_text in enumerate(self.messages):
            message_event = UiPathConversationMessageEvent(
                message_id=f"msg-{idx}",
                start=UiPathConversationMessageStartEvent(
                    role="assistant",
                    timestamp="2025-01-01T00:00:00.000Z",
                ),
            )
            yield UiPathRuntimeMessageEvent(payload=message_event)

        # Final result at the end of streaming
        yield UiPathRuntimeResult(
            status=UiPathRuntimeStatus.SUCCESSFUL,
            output={"messages": self.messages},
        )

    async def get_schema(self) -> UiPathRuntimeSchema:
        raise NotImplementedError()


class SuspendingMockRuntime:
    """Mock runtime that can suspend with API triggers."""

    def __init__(
        self,
        suspend_at_message: int | None = None,
    ) -> None:
        self.suspend_at_message = suspend_at_message

    async def dispose(self) -> None:
        pass

    async def execute(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathExecuteOptions | None = None,
    ) -> UiPathRuntimeResult:
        """Fallback execute path."""
        return UiPathRuntimeResult(
            status=UiPathRuntimeStatus.SUCCESSFUL,
            output={"mode": "execute"},
        )

    async def stream(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathStreamOptions | None = None,
    ) -> AsyncGenerator[UiPathRuntimeEvent, None]:
        """Stream events with potential API trigger suspension."""
        is_resume = options and options.resume

        if not is_resume:
            # Initial execution - yield message and then suspend
            message_event = UiPathConversationMessageEvent(
                message_id="msg-0",
                start=UiPathConversationMessageStartEvent(
                    role="assistant",
                    timestamp="2025-01-01T00:00:00.000Z",
                ),
            )
            yield UiPathRuntimeMessageEvent(payload=message_event)

            if self.suspend_at_message is not None:
                # Suspend with API trigger
                trigger = UiPathResumeTrigger(
                    interrupt_id="interrupt-1",
                    trigger_type=UiPathResumeTriggerType.API,
                    payload={"action": "confirm_tool_call"},
                )
                yield UiPathRuntimeResult(
                    status=UiPathRuntimeStatus.SUSPENDED,
                    trigger=trigger,
                    triggers=[trigger],
                )
                return
        else:
            # Resumed execution - yield another message and complete
            message_event = UiPathConversationMessageEvent(
                message_id="msg-1",
                start=UiPathConversationMessageStartEvent(
                    role="assistant",
                    timestamp="2025-01-01T00:00:01.000Z",
                ),
            )
            yield UiPathRuntimeMessageEvent(payload=message_event)

        # Final successful result
        yield UiPathRuntimeResult(
            status=UiPathRuntimeStatus.SUCCESSFUL,
            output={"resumed": is_resume, "input": input},
        )

    async def get_schema(self) -> UiPathRuntimeSchema:
        raise NotImplementedError()


@pytest.mark.asyncio
async def test_chat_runtime_streams_and_emits_messages():
    """UiPathChatRuntime should stream events and emit message events to bridge."""

    runtime_impl = StreamingMockRuntime(
        messages=["Hello", "How are you?", "Goodbye"],
    )
    bridge = make_chat_bridge_mock()

    chat_runtime = UiPathChatRuntime(
        delegate=runtime_impl,
        chat_bridge=bridge,
    )

    result = await chat_runtime.execute({})

    await chat_runtime.dispose()

    # Result propagation
    assert isinstance(result, UiPathRuntimeResult)
    assert result.status == UiPathRuntimeStatus.SUCCESSFUL
    assert result.output == {"messages": ["Hello", "How are you?", "Goodbye"]}

    # Bridge lifecycle
    cast(AsyncMock, bridge.connect).assert_awaited_once()
    cast(AsyncMock, bridge.disconnect).assert_awaited_once()

    assert cast(AsyncMock, bridge.emit_message_event).await_count == 3

    # Verify message events were passed as UiPathConversationMessageEvent objects
    calls = cast(AsyncMock, bridge.emit_message_event).await_args_list
    assert isinstance(calls[0][0][0], UiPathConversationMessageEvent)
    assert calls[0][0][0].message_id == "msg-0"
    assert isinstance(calls[1][0][0], UiPathConversationMessageEvent)
    assert calls[1][0][0].message_id == "msg-1"
    assert isinstance(calls[2][0][0], UiPathConversationMessageEvent)
    assert calls[2][0][0].message_id == "msg-2"


@pytest.mark.asyncio
async def test_chat_runtime_stream_yields_all_events():
    """UiPathChatRuntime.stream() should yield all events from delegate."""

    runtime_impl = StreamingMockRuntime(
        messages=["Message 1", "Message 2"],
    )
    bridge = make_chat_bridge_mock()

    chat_runtime = UiPathChatRuntime(
        delegate=runtime_impl,
        chat_bridge=bridge,
    )

    events = []
    async for event in chat_runtime.stream({}):
        events.append(event)

    await chat_runtime.dispose()

    # Should have 2 message events + 1 final result
    assert len(events) == 3
    assert isinstance(events[0], UiPathRuntimeMessageEvent)
    assert isinstance(events[1], UiPathRuntimeMessageEvent)
    assert isinstance(events[2], UiPathRuntimeResult)

    # Bridge methods called
    cast(AsyncMock, bridge.connect).assert_awaited_once()
    cast(AsyncMock, bridge.disconnect).assert_awaited_once()
    assert cast(AsyncMock, bridge.emit_message_event).await_count == 2


@pytest.mark.asyncio
async def test_chat_runtime_handles_errors():
    """On unexpected errors, UiPathChatRuntime should propagate them."""

    runtime_impl = StreamingMockRuntime(
        messages=["Message"],
        error_in_stream=True,
    )
    bridge = make_chat_bridge_mock()

    chat_runtime = UiPathChatRuntime(
        delegate=runtime_impl,
        chat_bridge=bridge,
    )

    # Error should propagate
    with pytest.raises(RuntimeError, match="Stream blew up"):
        await chat_runtime.execute({})

    cast(AsyncMock, bridge.connect).assert_awaited_once()


@pytest.mark.asyncio
async def test_chat_runtime_dispose_calls_disconnect():
    """dispose() should call chat bridge disconnect."""

    runtime_impl = StreamingMockRuntime(messages=["Message"])
    bridge = make_chat_bridge_mock()

    chat_runtime = UiPathChatRuntime(
        delegate=runtime_impl,
        chat_bridge=bridge,
    )

    await chat_runtime.dispose()

    # Bridge disconnect should be called
    cast(AsyncMock, bridge.disconnect).assert_awaited_once()


@pytest.mark.asyncio
async def test_chat_runtime_dispose_suppresses_disconnect_errors():
    """Errors from chat_bridge.disconnect should be suppressed."""

    runtime_impl = StreamingMockRuntime(messages=["Message"])
    bridge = make_chat_bridge_mock()
    cast(AsyncMock, bridge.disconnect).side_effect = RuntimeError("disconnect failed")

    chat_runtime = UiPathChatRuntime(
        delegate=runtime_impl,
        chat_bridge=bridge,
    )

    await chat_runtime.dispose()

    cast(AsyncMock, bridge.disconnect).assert_awaited_once()


@pytest.mark.asyncio
async def test_chat_runtime_handles_api_trigger_suspension():
    """UiPathChatRuntime should intercept suspensions and resume execution."""

    runtime_impl = SuspendingMockRuntime(suspend_at_message=0)
    bridge = make_chat_bridge_mock()

    cast(AsyncMock, bridge.wait_for_resume).return_value = {"approved": True}

    chat_runtime = UiPathChatRuntime(
        delegate=runtime_impl,
        chat_bridge=bridge,
    )

    result = await chat_runtime.execute({})

    await chat_runtime.dispose()

    # Result should be SUCCESSFUL
    assert isinstance(result, UiPathRuntimeResult)
    assert result.status == UiPathRuntimeStatus.SUCCESSFUL
    assert result.output == {
        "resumed": True,
        "input": {"interrupt-1": {"approved": True}},
    }

    cast(AsyncMock, bridge.connect).assert_awaited_once()
    cast(AsyncMock, bridge.disconnect).assert_awaited_once()

    cast(AsyncMock, bridge.emit_interrupt_event).assert_awaited_once()
    cast(AsyncMock, bridge.wait_for_resume).assert_awaited_once()

    # Message events emitted (one before suspend, one after resume)
    assert cast(AsyncMock, bridge.emit_message_event).await_count == 2


@pytest.mark.asyncio
async def test_chat_runtime_yields_events_during_suspension_flow():
    """UiPathChatRuntime.stream() should not yield SUSPENDED result, only final result."""

    runtime_impl = SuspendingMockRuntime(suspend_at_message=0)
    bridge = make_chat_bridge_mock()

    # wait_for_resume returns approval data
    cast(AsyncMock, bridge.wait_for_resume).return_value = {"approved": True}

    chat_runtime = UiPathChatRuntime(
        delegate=runtime_impl,
        chat_bridge=bridge,
    )

    events = []
    async for event in chat_runtime.stream({}):
        events.append(event)

    await chat_runtime.dispose()

    # Should have 2 message events + 1 final SUCCESSFUL result
    # SUSPENDED result should NOT be yielded
    assert len(events) == 3
    assert isinstance(events[0], UiPathRuntimeMessageEvent)
    assert events[0].payload.message_id == "msg-0"
    assert isinstance(events[1], UiPathRuntimeMessageEvent)
    assert events[1].payload.message_id == "msg-1"
    assert isinstance(events[2], UiPathRuntimeResult)
    assert events[2].status == UiPathRuntimeStatus.SUCCESSFUL

    # Verify no SUSPENDED result was yielded
    for event in events:
        if isinstance(event, UiPathRuntimeResult):
            assert event.status != UiPathRuntimeStatus.SUSPENDED


class MultiTriggerMockRuntime:
    """Mock runtime that suspends with multiple API triggers."""

    def __init__(self) -> None:
        self.execution_count = 0

    async def dispose(self) -> None:
        pass

    async def execute(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathExecuteOptions | None = None,
    ) -> UiPathRuntimeResult:
        """Execute with multiple trigger suspension."""
        result: UiPathRuntimeResult | None = None
        async for event in self.stream(input, cast(UiPathStreamOptions, options)):
            if isinstance(event, UiPathRuntimeResult):
                result = event
        return (
            result
            if result
            else UiPathRuntimeResult(status=UiPathRuntimeStatus.SUCCESSFUL)
        )

    async def stream(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathStreamOptions | None = None,
    ) -> AsyncGenerator[UiPathRuntimeEvent, None]:
        """Stream with multiple trigger suspension."""
        self.execution_count += 1
        is_resume = options and options.resume

        if not is_resume:
            # Initial execution - suspend with 3 API triggers
            trigger_a = UiPathResumeTrigger(
                interrupt_id="email-confirm",
                trigger_type=UiPathResumeTriggerType.API,
                payload={"action": "send_email", "to": "user@example.com"},
            )
            trigger_b = UiPathResumeTrigger(
                interrupt_id="file-delete",
                trigger_type=UiPathResumeTriggerType.API,
                payload={"action": "delete_file", "path": "/logs/old.txt"},
            )
            trigger_c = UiPathResumeTrigger(
                interrupt_id="api-call",
                trigger_type=UiPathResumeTriggerType.API,
                payload={"action": "call_api", "endpoint": "/users"},
            )

            yield UiPathRuntimeResult(
                status=UiPathRuntimeStatus.SUSPENDED,
                trigger=trigger_a,
                triggers=[trigger_a, trigger_b, trigger_c],
            )
        else:
            # Resumed - verify all triggers resolved
            assert input is not None
            assert "email-confirm" in input
            assert "file-delete" in input
            assert "api-call" in input

            yield UiPathRuntimeResult(
                status=UiPathRuntimeStatus.SUCCESSFUL,
                output={"resumed": True, "input": input},
            )

    async def get_schema(self) -> UiPathRuntimeSchema:
        raise NotImplementedError()


class MixedTriggerMockRuntime:
    """Mock runtime that suspends with mixed trigger types (API + non-API)."""

    async def dispose(self) -> None:
        pass

    async def execute(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathExecuteOptions | None = None,
    ) -> UiPathRuntimeResult:
        """Execute with mixed triggers."""
        result: UiPathRuntimeResult | None = None
        async for event in self.stream(input, cast(UiPathStreamOptions, options)):
            if isinstance(event, UiPathRuntimeResult):
                result = event
        return (
            result
            if result
            else UiPathRuntimeResult(status=UiPathRuntimeStatus.SUCCESSFUL)
        )

    async def stream(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathStreamOptions | None = None,
    ) -> AsyncGenerator[UiPathRuntimeEvent, None]:
        """Stream with mixed trigger types."""
        is_resume = options and options.resume

        if not is_resume:
            # Initial execution - 2 API + 1 QUEUE trigger
            trigger_a = UiPathResumeTrigger(
                interrupt_id="email-confirm",
                trigger_type=UiPathResumeTriggerType.API,
                payload={"action": "send_email"},
            )
            trigger_b = UiPathResumeTrigger(
                interrupt_id="file-delete",
                trigger_type=UiPathResumeTriggerType.API,
                payload={"action": "delete_file"},
            )
            trigger_c = UiPathResumeTrigger(
                interrupt_id="queue-item",
                trigger_type=UiPathResumeTriggerType.QUEUE_ITEM,
                payload={"queue": "inbox", "item": "123"},
            )

            yield UiPathRuntimeResult(
                status=UiPathRuntimeStatus.SUSPENDED,
                trigger=trigger_a,
                triggers=[trigger_a, trigger_b, trigger_c],
            )
        else:
            # Resumed - verify only API triggers resolved
            assert input is not None
            assert "email-confirm" in input
            assert "file-delete" in input
            # QUEUE trigger should NOT be in input (handled externally)

            # Suspend again with only QUEUE trigger
            trigger_c = UiPathResumeTrigger(
                interrupt_id="queue-item",
                trigger_type=UiPathResumeTriggerType.QUEUE_ITEM,
                payload={"queue": "inbox", "item": "123"},
            )

            yield UiPathRuntimeResult(
                status=UiPathRuntimeStatus.SUSPENDED,
                trigger=trigger_c,
                triggers=[trigger_c],
            )

    async def get_schema(self) -> UiPathRuntimeSchema:
        raise NotImplementedError()


@pytest.mark.asyncio
async def test_chat_runtime_handles_multiple_api_triggers():
    """ChatRuntime should resolve all API triggers before resuming."""

    runtime_impl = MultiTriggerMockRuntime()
    bridge = make_chat_bridge_mock()

    # Bridge returns approval for each trigger
    cast(AsyncMock, bridge.wait_for_resume).side_effect = [
        {"approved": True},  # email-confirm
        {"approved": True},  # file-delete
        {"approved": True},  # api-call
    ]

    chat_runtime = UiPathChatRuntime(
        delegate=runtime_impl,
        chat_bridge=bridge,
    )

    result = await chat_runtime.execute({})

    await chat_runtime.dispose()

    # Result should be SUCCESSFUL
    assert result.status == UiPathRuntimeStatus.SUCCESSFUL
    assert isinstance(result.output, dict)
    assert result.output["resumed"] is True

    # Verify all 3 triggers were wrapped with interrupt_ids
    resume_input = cast(dict[str, Any], result.output["input"])
    assert "email-confirm" in resume_input
    assert "file-delete" in resume_input
    assert "api-call" in resume_input
    assert resume_input["email-confirm"] == {"approved": True}
    assert resume_input["file-delete"] == {"approved": True}
    assert resume_input["api-call"] == {"approved": True}

    # Bridge should have been called 3 times (once per trigger)
    assert cast(AsyncMock, bridge.emit_interrupt_event).await_count == 3
    assert cast(AsyncMock, bridge.wait_for_resume).await_count == 3

    # Verify each emit_interrupt_event received a trigger
    emit_calls = cast(AsyncMock, bridge.emit_interrupt_event).await_args_list
    assert emit_calls[0][0][0].interrupt_id == "email-confirm"
    assert emit_calls[1][0][0].interrupt_id == "file-delete"
    assert emit_calls[2][0][0].interrupt_id == "api-call"


@pytest.mark.asyncio
async def test_chat_runtime_filters_non_api_triggers():
    """ChatRuntime should only handle API triggers, pass through non-API triggers."""

    runtime_impl = MixedTriggerMockRuntime()
    bridge = make_chat_bridge_mock()

    # Bridge returns approval for API triggers only
    cast(AsyncMock, bridge.wait_for_resume).side_effect = [
        {"approved": True},  # email-confirm
        {"approved": True},  # file-delete
    ]

    chat_runtime = UiPathChatRuntime(
        delegate=runtime_impl,
        chat_bridge=bridge,
    )

    result = await chat_runtime.execute({})

    await chat_runtime.dispose()

    # Result should be SUSPENDED with QUEUE trigger (non-API)
    assert result.status == UiPathRuntimeStatus.SUSPENDED
    assert result.triggers is not None
    assert len(result.triggers) == 1
    assert result.triggers[0].interrupt_id == "queue-item"
    assert result.triggers[0].trigger_type == UiPathResumeTriggerType.QUEUE_ITEM

    # Bridge should have been called only 2 times (for 2 API triggers)
    assert cast(AsyncMock, bridge.emit_interrupt_event).await_count == 2
    assert cast(AsyncMock, bridge.wait_for_resume).await_count == 2

    # Verify only API triggers were emitted
    emit_calls = cast(AsyncMock, bridge.emit_interrupt_event).await_args_list
    assert emit_calls[0][0][0].interrupt_id == "email-confirm"
    assert emit_calls[0][0][0].trigger_type == UiPathResumeTriggerType.API
    assert emit_calls[1][0][0].interrupt_id == "file-delete"
    assert emit_calls[1][0][0].trigger_type == UiPathResumeTriggerType.API
