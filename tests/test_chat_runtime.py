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
