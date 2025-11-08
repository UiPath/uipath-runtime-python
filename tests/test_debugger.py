"""Tests for UiPathDebugRuntime with mocked runtime and debug bridge."""

from __future__ import annotations

from typing import List, Sequence, cast
from unittest.mock import AsyncMock, Mock

import pytest

from uipath.runtime import (
    UiPathBaseRuntime,
    UiPathBreakpointResult,
    UiPathRuntimeContext,
    UiPathRuntimeResult,
    UiPathRuntimeStatus,
    UiPathStreamNotSupportedError,
)
from uipath.runtime.debug import (
    UiPathDebugBridge,
    UiPathDebugQuitError,
    UiPathDebugRuntime,
)
from uipath.runtime.events import UiPathRuntimeStateEvent


def make_debug_bridge_mock() -> UiPathDebugBridge:
    """Create a debug bridge mock with all methods that UiPathDebugRuntime uses.

    We use `spec=UiPathDebugBridge` so invalid attributes raise at runtime,
    but still operate as a unittest.mock.Mock with AsyncMock methods.
    """
    bridge_mock: Mock = Mock(spec=UiPathDebugBridge)

    bridge_mock.connect = AsyncMock()
    bridge_mock.disconnect = AsyncMock()
    bridge_mock.emit_execution_started = AsyncMock()
    bridge_mock.emit_execution_completed = AsyncMock()
    bridge_mock.emit_execution_error = AsyncMock()
    bridge_mock.emit_breakpoint_hit = AsyncMock()
    bridge_mock.emit_state_update = AsyncMock()
    bridge_mock.wait_for_resume = AsyncMock()

    bridge_mock.get_breakpoints = Mock(return_value=["node-1"])

    return cast(UiPathDebugBridge, bridge_mock)


class StreamingMockRuntime(UiPathBaseRuntime):
    """Mock runtime that streams state events, breakpoint hits and a final result."""

    def __init__(
        self,
        context: UiPathRuntimeContext,
        node_sequence: Sequence[str],
        *,
        stream_unsupported: bool = False,
        error_in_stream: bool = False,
    ) -> None:
        super().__init__(context)
        self.node_sequence: List[str] = list(node_sequence)
        self.stream_unsupported: bool = stream_unsupported
        self.error_in_stream: bool = error_in_stream

        self.execute_called: bool = False
        self.validate_called: bool = False
        self.cleanup_called: bool = False

    async def validate(self) -> None:
        self.validate_called = True

    async def cleanup(self) -> None:
        self.cleanup_called = True

    async def execute(self) -> UiPathRuntimeResult:
        """Fallback execute path (used when streaming is not supported)."""
        self.execute_called = True
        return UiPathRuntimeResult(
            status=UiPathRuntimeStatus.SUCCESSFUL,
            output={"mode": "execute"},
        )

    async def stream(self):
        """Async generator yielding state events, breakpoint events, and final result."""
        if self.stream_unsupported:
            raise UiPathStreamNotSupportedError("Streaming not supported")

        if self.error_in_stream:
            raise RuntimeError("Stream blew up")

        for idx, node in enumerate(self.node_sequence):
            # 1) Always emit a state update event
            yield UiPathRuntimeStateEvent(
                execution_id=self.context.execution_id,
                node_name=node,
                payload={"index": idx, "node": node},
            )

            # 2) Check for breakpoints on this node
            breakpoints = self.context.breakpoints
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


@pytest.mark.asyncio
async def test_debug_runtime_streams_and_handles_breakpoints_and_state():
    """UiPathDebugRuntime should stream events, handle breakpoints and state updates."""

    context = UiPathRuntimeContext(execution_id="exec-stream")

    runtime_impl = StreamingMockRuntime(
        context,
        node_sequence=["node-1", "node-2", "node-3"],
    )
    bridge = make_debug_bridge_mock()

    # Initial resume (before streaming) + resume after breakpoint hit
    cast(AsyncMock, bridge.wait_for_resume).side_effect = [None, None]
    cast(Mock, bridge.get_breakpoints).return_value = ["node-2"]

    debug_runtime = UiPathDebugRuntime(
        context=context,
        delegate=runtime_impl,
        debug_bridge=bridge,
    )

    result = await debug_runtime.execute()

    # Result propagation
    assert isinstance(result, UiPathRuntimeResult)
    assert result.status == UiPathRuntimeStatus.SUCCESSFUL
    assert result.output == {"visited_nodes": ["node-1", "node-2", "node-3"]}
    assert context.result is result

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

    # Breakpoints applied to inner runtime context
    assert runtime_impl.context.breakpoints == ["node-2"]
    # After resume, debug runtime should set resume flag
    assert runtime_impl.context.resume is True


@pytest.mark.asyncio
async def test_debug_runtime_falls_back_when_stream_not_supported():
    """If runtime raises UiPathStreamNotSupportedError, we fall back to execute()."""

    context = UiPathRuntimeContext(execution_id="exec-fallback")

    runtime_impl = StreamingMockRuntime(
        context,
        node_sequence=["node-1"],
        stream_unsupported=True,
    )
    bridge = make_debug_bridge_mock()

    # Initial resume (even if streaming fails, debug runtime will still call it once)
    cast(AsyncMock, bridge.wait_for_resume).return_value = None

    debug_runtime = UiPathDebugRuntime(
        context=context,
        delegate=runtime_impl,
        debug_bridge=bridge,
    )

    result = await debug_runtime.execute()

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

    context = UiPathRuntimeContext(execution_id="exec-quit")

    runtime_impl = StreamingMockRuntime(
        context,
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
        context=context,
        delegate=runtime_impl,
        debug_bridge=bridge,
    )

    result = await debug_runtime.execute()

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

    context = UiPathRuntimeContext(execution_id="exec-error")

    # This runtime will raise an error as soon as stream() is used
    runtime_impl = StreamingMockRuntime(
        context,
        node_sequence=["node-1"],
        error_in_stream=True,
    )
    bridge = make_debug_bridge_mock()
    cast(AsyncMock, bridge.wait_for_resume).return_value = None

    debug_runtime = UiPathDebugRuntime(
        context=context,
        delegate=runtime_impl,
        debug_bridge=bridge,
    )

    with pytest.raises(RuntimeError, match="Stream blew up"):
        await debug_runtime.execute()

    # Context should be marked FAULTED
    assert context.result is not None
    assert context.result.status == UiPathRuntimeStatus.FAULTED

    # Error should be emitted to debug bridge
    cast(AsyncMock, bridge.emit_execution_error).assert_awaited_once()
    # Completion should not be emitted in error path
    cast(AsyncMock, bridge.emit_execution_completed).assert_not_awaited()


@pytest.mark.asyncio
async def test_debug_runtime_cleanup_calls_inner_cleanup_and_disconnect():
    """cleanup() should call inner runtime cleanup and debug bridge disconnect."""

    context = UiPathRuntimeContext(execution_id="exec-cleanup")

    runtime_impl = StreamingMockRuntime(context, node_sequence=["node-1"])
    bridge = make_debug_bridge_mock()

    debug_runtime = UiPathDebugRuntime(
        context=context,
        delegate=runtime_impl,
        debug_bridge=bridge,
    )

    await debug_runtime.cleanup()

    assert runtime_impl.cleanup_called is True
    cast(AsyncMock, bridge.disconnect).assert_awaited_once()


@pytest.mark.asyncio
async def test_debug_runtime_cleanup_suppresses_disconnect_errors():
    """Errors from debug_bridge.disconnect should be suppressed, inner cleanup still runs."""

    context = UiPathRuntimeContext(execution_id="exec-cleanup-disconnect-error")

    runtime_impl = StreamingMockRuntime(context, node_sequence=["node-1"])
    bridge = make_debug_bridge_mock()
    cast(AsyncMock, bridge.disconnect).side_effect = RuntimeError("disconnect failed")

    debug_runtime = UiPathDebugRuntime(
        context=context,
        delegate=runtime_impl,
        debug_bridge=bridge,
    )

    # No exception should bubble up from cleanup()
    await debug_runtime.cleanup()

    assert runtime_impl.cleanup_called is True
    cast(AsyncMock, bridge.disconnect).assert_awaited_once()


@pytest.mark.asyncio
async def test_debug_runtime_cleanup_propagates_inner_cleanup_error_but_still_disconnects():
    """If inner runtime cleanup fails, the error bubbles up but disconnect is still attempted."""

    context = UiPathRuntimeContext(execution_id="exec-cleanup-inner-error")

    runtime_impl = StreamingMockRuntime(context, node_sequence=["node-1"])
    bridge = make_debug_bridge_mock()

    async def failing_cleanup() -> None:
        runtime_impl.cleanup_called = True
        raise RuntimeError("inner cleanup failed")

    runtime_impl.cleanup = failing_cleanup  # type: ignore[method-assign]

    debug_runtime = UiPathDebugRuntime(
        context=context,
        delegate=runtime_impl,
        debug_bridge=bridge,
    )

    with pytest.raises(RuntimeError, match="inner cleanup failed"):
        await debug_runtime.cleanup()

    assert runtime_impl.cleanup_called is True
    cast(AsyncMock, bridge.disconnect).assert_awaited_once()
