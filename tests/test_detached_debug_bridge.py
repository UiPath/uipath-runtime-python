"""Tests for DetachedDebugBridge."""

from __future__ import annotations

import asyncio

import pytest

from uipath.runtime.debug import (
    DetachedDebugBridge,
    UiPathBreakpointResult,
    UiPathDebugProtocol,
)
from uipath.runtime.events import UiPathRuntimeStateEvent
from uipath.runtime.result import UiPathRuntimeResult, UiPathRuntimeStatus


def test_detached_bridge_satisfies_debug_protocol():
    """DetachedDebugBridge must be usable wherever UiPathDebugProtocol is expected."""
    bridge: UiPathDebugProtocol = DetachedDebugBridge()
    assert bridge is not None


@pytest.mark.asyncio
async def test_connect_and_disconnect_are_noops():
    """Lifecycle methods must complete without raising."""
    bridge = DetachedDebugBridge()
    await bridge.connect()
    await bridge.disconnect()


@pytest.mark.asyncio
async def test_all_emit_methods_are_noops():
    """Emit methods must not raise and must not require any external state."""
    bridge = DetachedDebugBridge()

    state_event = UiPathRuntimeStateEvent(node_name="node-x", payload={})
    breakpoint_result = UiPathBreakpointResult(
        breakpoint_node="node-x",
        breakpoint_type="before",
        next_nodes=[],
        current_state={},
    )
    runtime_result = UiPathRuntimeResult(
        status=UiPathRuntimeStatus.SUCCESSFUL,
        output={},
    )

    await bridge.emit_execution_started()
    await bridge.emit_state_update(state_event)
    await bridge.emit_breakpoint_hit(breakpoint_result)
    await bridge.emit_execution_suspended(runtime_result)
    await bridge.emit_execution_resumed({"any": "data"})
    await bridge.emit_execution_completed(runtime_result)
    await bridge.emit_execution_error("boom")


@pytest.mark.asyncio
async def test_wait_for_resume_returns_immediately():
    """The runtime's initial paused gate calls this — it must release without blocking."""
    bridge = DetachedDebugBridge()
    # Fails the test if the call hangs for any reason.
    await asyncio.wait_for(bridge.wait_for_resume(), timeout=1.0)


@pytest.mark.asyncio
async def test_wait_for_terminate_blocks_forever():
    """Termination can never arrive on a detached bridge — the coroutine must not complete."""
    bridge = DetachedDebugBridge()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(bridge.wait_for_terminate(), timeout=0.1)


def test_get_breakpoints_returns_empty_list():
    """Empty list means 'no breakpoints' — the runtime's normal flow then skips suspension."""
    bridge = DetachedDebugBridge()
    assert bridge.get_breakpoints() == []
