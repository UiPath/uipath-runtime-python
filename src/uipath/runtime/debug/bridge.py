"""Abstract debug bridge interface."""

from abc import ABC, abstractmethod
from typing import Any, Literal

from uipath.runtime import (
    UiPathBreakpointResult,
    UiPathRuntimeResult,
)
from uipath.runtime.events import UiPathRuntimeStateEvent


class UiPathDebugBridge(ABC):
    """Abstract interface for debug communication.

    Implementations: SignalR, Console, WebSocket, etc.
    """

    @abstractmethod
    async def connect(self) -> None:
        """Establish connection to debugger."""
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        """Close connection to debugger."""
        pass

    @abstractmethod
    async def emit_execution_started(self, **kwargs) -> None:
        """Notify debugger that execution started."""
        pass

    @abstractmethod
    async def emit_state_update(self, state_event: UiPathRuntimeStateEvent) -> None:
        """Notify debugger of runtime state update."""
        pass

    @abstractmethod
    async def emit_breakpoint_hit(
        self, breakpoint_result: UiPathBreakpointResult
    ) -> None:
        """Notify debugger that a breakpoint was hit."""
        pass

    @abstractmethod
    async def emit_execution_completed(
        self,
        runtime_result: UiPathRuntimeResult,
    ) -> None:
        """Notify debugger that execution completed."""
        pass

    @abstractmethod
    async def emit_execution_error(
        self,
        error: str,
    ) -> None:
        """Notify debugger that an error occurred."""
        pass

    @abstractmethod
    async def wait_for_resume(self) -> Any:
        """Wait for resume command from debugger."""
        pass

    @abstractmethod
    def get_breakpoints(self) -> list[str] | Literal["*"]:
        """Get nodes to suspend execution at.

        Returns:
            List of node names to suspend at, or ["*"] for all nodes (step mode)
        """
        pass
