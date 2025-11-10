"""Abstract debug bridge interface."""

from typing import Any, Literal, Protocol

from uipath.runtime import (
    UiPathBreakpointResult,
    UiPathRuntimeResult,
)
from uipath.runtime.events import UiPathRuntimeStateEvent


class UiPathDebugBridgeProtocol(Protocol):
    """Abstract interface for debug communication.

    Implementations: SignalR, Console, WebSocket, etc.
    """

    async def connect(self) -> None:
        """Establish connection to debugger."""
        ...

    async def disconnect(self) -> None:
        """Close connection to debugger."""
        ...

    async def emit_execution_started(self, **kwargs) -> None:
        """Notify debugger that execution started."""
        ...

    async def emit_state_update(self, state_event: UiPathRuntimeStateEvent) -> None:
        """Notify debugger of runtime state update."""
        ...

    async def emit_breakpoint_hit(
        self, breakpoint_result: UiPathBreakpointResult
    ) -> None:
        """Notify debugger that a breakpoint was hit."""
        ...

    async def emit_execution_completed(
        self,
        runtime_result: UiPathRuntimeResult,
    ) -> None:
        """Notify debugger that execution completed."""
        ...

    async def emit_execution_error(
        self,
        error: str,
    ) -> None:
        """Notify debugger that an error occurred."""
        ...

    async def wait_for_resume(self) -> Any:
        """Wait for resume command from debugger."""
        ...

    def get_breakpoints(self) -> list[str] | Literal["*"]:
        """Get nodes to suspend execution at.

        Returns:
            List of node names to suspend at, or ["*"] for all nodes (step mode)
        """
        ...
