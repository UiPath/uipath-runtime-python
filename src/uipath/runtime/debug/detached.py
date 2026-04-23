"""Detached debug bridge — satisfies `UiPathDebugProtocol` without attaching a debugger."""

import asyncio
from typing import Any, Literal

from uipath.runtime.debug.breakpoint import UiPathBreakpointResult
from uipath.runtime.events import UiPathRuntimeStateEvent
from uipath.runtime.result import UiPathRuntimeResult


class DetachedDebugBridge:
    """Debug bridge used when no debugger is attached.

    Implements `UiPathDebugProtocol` so the debug runtime stack keeps wrapping
    uniformly, but all hooks are no-ops. `wait_for_resume` returns immediately
    so the runtime's initial paused-state gate releases without blocking;
    `wait_for_terminate` blocks forever because termination can never arrive
    through a bridge that isn't connected to anything.
    """

    async def connect(self) -> None:
        """No-op — nothing to connect to when detached."""
        pass

    async def disconnect(self) -> None:
        """No-op — no connection to tear down."""
        pass

    async def emit_execution_started(self, **kwargs: Any) -> None:
        """No-op — no debugger is listening."""
        pass

    async def emit_state_update(self, state_event: UiPathRuntimeStateEvent) -> None:
        """No-op — no debugger is listening."""
        pass

    async def emit_breakpoint_hit(
        self, breakpoint_result: UiPathBreakpointResult
    ) -> None:
        """No-op — no debugger is listening."""
        pass

    async def emit_execution_suspended(
        self, runtime_result: UiPathRuntimeResult
    ) -> None:
        """No-op — no debugger is listening."""
        pass

    async def emit_execution_resumed(self, resume_data: Any) -> None:
        """No-op — no debugger is listening."""
        pass

    async def emit_execution_completed(
        self, runtime_result: UiPathRuntimeResult
    ) -> None:
        """No-op — no debugger is listening."""
        pass

    async def emit_execution_error(self, error: str) -> None:
        """No-op — no debugger is listening."""
        pass

    async def wait_for_resume(self) -> Any:
        """Return immediately — the runtime's initial paused gate releases without a debugger."""
        return None

    async def wait_for_terminate(self) -> None:
        """Block forever — termination cannot arrive when no debugger is attached."""
        await asyncio.Event().wait()

    def get_breakpoints(self) -> list[str] | Literal["*"]:
        """Return an empty breakpoint list so the runtime never suspends."""
        return []
