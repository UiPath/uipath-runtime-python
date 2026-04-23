"""Integration test: UiPathDebugRuntime must not block under DetachedDebugBridge.

If this ever hangs or times out, the detached path has regressed — the scenario
this bridge exists to enable has broken.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncGenerator

import pytest

from uipath.runtime import (
    UiPathExecuteOptions,
    UiPathRuntimeResult,
    UiPathRuntimeStatus,
    UiPathStreamNotSupportedError,
    UiPathStreamOptions,
)
from uipath.runtime.debug import DetachedDebugBridge, UiPathDebugRuntime
from uipath.runtime.events import UiPathRuntimeEvent, UiPathRuntimeStateEvent
from uipath.runtime.schema import UiPathRuntimeSchema


class TrivialStreamingRuntime:
    """Streams one state event then a final successful result."""

    async def dispose(self) -> None:
        pass

    async def execute(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathExecuteOptions | None = None,
    ) -> UiPathRuntimeResult:
        return UiPathRuntimeResult(
            status=UiPathRuntimeStatus.SUCCESSFUL,
            output={"mode": "execute"},
        )

    async def stream(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathStreamOptions | None = None,
    ) -> AsyncGenerator[UiPathRuntimeEvent, None]:
        yield UiPathRuntimeStateEvent(node_name="node-1", payload={"i": 0})
        yield UiPathRuntimeResult(
            status=UiPathRuntimeStatus.SUCCESSFUL,
            output={"done": True},
        )

    async def get_schema(self) -> UiPathRuntimeSchema:
        raise NotImplementedError()


class NonStreamingRuntime:
    """Raises UiPathStreamNotSupportedError — forces the execute() fallback path."""

    def __init__(self) -> None:
        self.execute_called = False

    async def dispose(self) -> None:
        pass

    async def execute(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathExecuteOptions | None = None,
    ) -> UiPathRuntimeResult:
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
        raise UiPathStreamNotSupportedError("nope")
        yield  # pragma: no cover — makes this an async generator

    async def get_schema(self) -> UiPathRuntimeSchema:
        raise NotImplementedError()


@pytest.mark.asyncio
async def test_debug_runtime_streams_to_completion_under_detached_bridge():
    """The detached bridge must not block the runtime's startup wait-for-resume gate."""
    debug_runtime = UiPathDebugRuntime(
        delegate=TrivialStreamingRuntime(),
        debug_bridge=DetachedDebugBridge(),
    )

    try:
        result = await asyncio.wait_for(debug_runtime.execute({}), timeout=5.0)
    finally:
        await debug_runtime.dispose()

    assert isinstance(result, UiPathRuntimeResult)
    assert result.status == UiPathRuntimeStatus.SUCCESSFUL
    assert result.output == {"done": True}


@pytest.mark.asyncio
async def test_debug_runtime_execute_fallback_completes_under_detached_bridge():
    """Fallback path (stream-unsupported delegates) must also not block."""
    delegate = NonStreamingRuntime()
    debug_runtime = UiPathDebugRuntime(
        delegate=delegate,
        debug_bridge=DetachedDebugBridge(),
    )

    try:
        result = await asyncio.wait_for(debug_runtime.execute({}), timeout=5.0)
    finally:
        await debug_runtime.dispose()

    assert delegate.execute_called is True
    assert result.status == UiPathRuntimeStatus.SUCCESSFUL
