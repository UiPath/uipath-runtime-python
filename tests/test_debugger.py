"""Tests for UiPathDebugRuntime with mocked runtime and debug bridge."""

from __future__ import annotations

from typing import Any, AsyncGenerator, Sequence, cast
from unittest.mock import AsyncMock, Mock

import pytest

from uipath.runtime import (
    UiPathBreakpointResult,
    UiPathExecuteOptions,
    UiPathRuntimeContext,
    UiPathRuntimeResult,
    UiPathRuntimeStatus,
    UiPathStreamNotSupportedError,
    UiPathStreamOptions,
)
from uipath.runtime.debug import (
    UiPathDebugProtocol,
    UiPathDebugQuitError,
    UiPathDebugRuntime,
)
from uipath.runtime.events import UiPathRuntimeEvent, UiPathRuntimeStateEvent
from uipath.runtime.schema import UiPathRuntimeSchema


def make_debug_bridge_mock() -> UiPathDebugProtocol:
    """Create a debug bridge mock with all methods that UiPathDebugRuntime uses.

    We use `spec=UiPathDebugBridge` so invalid attributes raise at runtime,
    but still operate as a unittest.mock.Mock with AsyncMock methods.
    """
    bridge_mock: Mock = Mock(spec=UiPathDebugProtocol)

    bridge_mock.connect = AsyncMock()
    bridge_mock.disconnect = AsyncMock()
    bridge_mock.emit_execution_started = AsyncMock()
    bridge_mock.emit_execution_completed = AsyncMock()
    bridge_mock.emit_execution_error = AsyncMock()
    bridge_mock.emit_breakpoint_hit = AsyncMock()
    bridge_mock.emit_state_update = AsyncMock()
    bridge_mock.wait_for_resume = AsyncMock()

    bridge_mock.get_breakpoints = Mock(return_value=["node-1"])

    return cast(UiPathDebugProtocol, bridge_mock)


class StreamingMockRuntime:
    """Mock runtime that streams state events, breakpoint hits and a final result."""

    def __init__(
        self,
        node_sequence: Sequence[str],
        *,
        stream_unsupported: bool = False,
        error_in_stream: bool = False,
    ) -> None:
        super().__init__()
        self.node_sequence: list[str] = list(node_sequence)
        self.stream_unsupported: bool = stream_unsupported
        self.error_in_stream: bool = error_in_stream

        self.execute_called: bool = False

    async def dispose(self) -> None:
        pass

    async def execute(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathExecuteOptions | None = None,
    ) -> UiPathRuntimeResult:
        """Fallback execute path (used when streaming is not supported)."""
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
        """Async generator yielding state events, breakpoint events, and final result."""
        if self.stream_unsupported:
            raise UiPathStreamNotSupportedError("Streaming not supported")

        if self.error_in_stream:
            raise RuntimeError("Stream blew up")

        for idx, node in enumerate(self.node_sequence):
            # 1) Always emit a state update event
            yield UiPathRuntimeStateEvent(
                node_name=node,
                payload={"index": idx, "node": node},
            )

            # 2) Check for breakpoints on this node
            if options:
                breakpoints = options.breakpoints
            else:
                breakpoints = []
            hit_breakpoint = False

            if breakpoints == "*":
                hit_breakpoint = True
            elif isinstance(breakpoints, list) and node in breakpoints:
                hit_breakpoint = True

            if hit_breakpoint:
                next_nodes = self.node_sequence[idx + 1 : idx + 2]  # at most one
                yield UiPathBreakpointResult(
                    breakpoint_node=node,
                    breakpoint_type="before",
                    next_nodes=next_nodes,
                    current_state={"node": node, "index": idx},
                )

        # 3) Final result at the end of streaming
        yield UiPathRuntimeResult(
            status=UiPathRuntimeStatus.SUCCESSFUL,
            output={"visited_nodes": self.node_sequence},
        )

    async def get_schema(self) -> UiPathRuntimeSchema:
        """NotImplemented."""

        raise NotImplementedError()


@pytest.mark.asyncio
async def test_debug_runtime_streams_and_handles_breakpoints_and_state():
    """UiPathDebugRuntime should stream events, handle breakpoints and state updates."""

    runtime_impl = StreamingMockRuntime(
        node_sequence=["node-1", "node-2", "node-3"],
    )
    bridge = make_debug_bridge_mock()

    # Initial resume (before streaming) + resume after breakpoint hit
    cast(AsyncMock, bridge.wait_for_resume).side_effect = [None, None]
    cast(Mock, bridge.get_breakpoints).return_value = ["node-2"]

    debug_runtime = UiPathDebugRuntime(
        delegate=runtime_impl,
        debug_bridge=bridge,
    )

    result = await debug_runtime.execute({})

    # Result propagation
    assert isinstance(result, UiPathRuntimeResult)
    assert result.status == UiPathRuntimeStatus.SUCCESSFUL
    assert result.output == {"visited_nodes": ["node-1", "node-2", "node-3"]}

    # Bridge lifecycle
    cast(AsyncMock, bridge.connect).assert_awaited_once()
    cast(AsyncMock, bridge.emit_execution_started).assert_awaited_once()
    cast(AsyncMock, bridge.emit_execution_completed).assert_awaited_once_with(result)

    # Streaming interactions
    assert cast(AsyncMock, bridge.emit_state_update).await_count >= 1
    cast(AsyncMock, bridge.emit_breakpoint_hit).assert_awaited()
    assert (
        cast(AsyncMock, bridge.wait_for_resume).await_count == 2
    )  # initial + after breakpoint


@pytest.mark.asyncio
async def test_debug_runtime_falls_back_when_stream_not_supported():
    """If runtime raises UiPathStreamNotSupportedError, we fall back to execute()."""

    runtime_impl = StreamingMockRuntime(
        node_sequence=["node-1"],
        stream_unsupported=True,
    )
    bridge = make_debug_bridge_mock()

    # Initial resume (even if streaming fails, debug runtime will still call it once)
    cast(AsyncMock, bridge.wait_for_resume).return_value = None

    debug_runtime = UiPathDebugRuntime(
        delegate=runtime_impl,
        debug_bridge=bridge,
    )

    result = await debug_runtime.execute({})

    # Fallback to execute() path
    assert runtime_impl.execute_called is True
    assert result.status == UiPathRuntimeStatus.SUCCESSFUL
    assert result.output == {"mode": "execute"}

    # Bridge interactions
    cast(AsyncMock, bridge.connect).assert_awaited_once()
    cast(AsyncMock, bridge.emit_execution_started).assert_awaited_once()
    cast(AsyncMock, bridge.emit_execution_completed).assert_awaited_once_with(result)

    # No streaming-specific events
    cast(AsyncMock, bridge.emit_state_update).assert_not_awaited()
    cast(AsyncMock, bridge.emit_breakpoint_hit).assert_not_awaited()


@pytest.mark.asyncio
async def test_debug_runtime_quit_creates_successful_result():
    """UiPathDebugRuntime should handle UiPathDebugQuitError and return SUCCESSFUL."""

    runtime_impl = StreamingMockRuntime(
        node_sequence=["node-quit"],
    )
    bridge = make_debug_bridge_mock()

    # First resume: initial start; second resume: at breakpoint -> raises quit
    cast(AsyncMock, bridge.wait_for_resume).side_effect = [
        None,
        UiPathDebugQuitError("quit"),
    ]
    cast(Mock, bridge.get_breakpoints).return_value = ["node-quit"]

    debug_runtime = UiPathDebugRuntime(
        delegate=runtime_impl,
        debug_bridge=bridge,
    )

    result = await debug_runtime.execute({})

    # Quit result is synthesized as SUCCESSFUL (no specific output required)
    assert isinstance(result, UiPathRuntimeResult)
    assert result.status == UiPathRuntimeStatus.SUCCESSFUL

    # emit_breakpoint_hit should have been called once
    cast(AsyncMock, bridge.emit_breakpoint_hit).assert_awaited()
    assert cast(AsyncMock, bridge.wait_for_resume).await_count == 2

    # Completion event emitted with synthesized result
    cast(AsyncMock, bridge.emit_execution_completed).assert_awaited_once_with(result)


@pytest.mark.asyncio
async def test_debug_runtime_execute_reports_errors_and_marks_faulted():
    """On unexpected errors, UiPathDebugRuntime should emit error and mark result FAULTED."""

    # This runtime will raise an error as soon as stream() is used
    runtime_impl = StreamingMockRuntime(
        node_sequence=["node-1"],
        error_in_stream=True,
    )
    bridge = make_debug_bridge_mock()
    cast(AsyncMock, bridge.wait_for_resume).return_value = None

    debug_runtime = UiPathDebugRuntime(
        delegate=runtime_impl,
        debug_bridge=bridge,
    )

    with pytest.raises(RuntimeError, match="Stream blew up"):
        with UiPathRuntimeContext.with_defaults() as ctx:
            ctx.result = await debug_runtime.execute(input=ctx.input)

    # Context should be marked FAULTED
    assert ctx.result is not None
    assert ctx.result.status == UiPathRuntimeStatus.FAULTED

    # Error should be emitted to debug bridge
    cast(AsyncMock, bridge.emit_execution_error).assert_awaited_once()
    # Completion should not be emitted in error path
    cast(AsyncMock, bridge.emit_execution_completed).assert_not_awaited()


@pytest.mark.asyncio
async def test_debug_runtime_dispose_calls_disconnect():
    """dispose() should call debug bridge disconnect."""

    runtime_impl = StreamingMockRuntime(node_sequence=["node-1"])
    bridge = make_debug_bridge_mock()

    debug_runtime = UiPathDebugRuntime(
        delegate=runtime_impl,
        debug_bridge=bridge,
    )

    await debug_runtime.dispose()

    cast(AsyncMock, bridge.disconnect).assert_awaited_once()


@pytest.mark.asyncio
async def test_debug_runtime_dispose_suppresses_disconnect_errors():
    """Errors from debug_bridge.disconnect should be suppressed."""

    runtime_impl = StreamingMockRuntime(node_sequence=["node-1"])
    bridge = make_debug_bridge_mock()
    cast(AsyncMock, bridge.disconnect).side_effect = RuntimeError("disconnect failed")

    debug_runtime = UiPathDebugRuntime(
        delegate=runtime_impl,
        debug_bridge=bridge,
    )

    # No exception should bubble up from dispose()
    await debug_runtime.dispose()

    cast(AsyncMock, bridge.disconnect).assert_awaited_once()
