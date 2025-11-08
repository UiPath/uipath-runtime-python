"""Debug runtime implementation."""

import logging
from typing import Generic, TypeVar

from uipath.runtime import (
    UiPathBaseRuntime,
    UiPathBreakpointResult,
    UiPathRuntimeContext,
    UiPathRuntimeResult,
    UiPathRuntimeStatus,
    UiPathStreamNotSupportedError,
)
from uipath.runtime.debug import UiPathDebugBridge, UiPathDebugQuitError
from uipath.runtime.events import (
    UiPathRuntimeStateEvent,
)

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=UiPathBaseRuntime)


class UiPathDebugRuntime(UiPathBaseRuntime, Generic[T]):
    """Specialized runtime for debug runs that streams events to a debug bridge."""

    def __init__(
        self,
        context: UiPathRuntimeContext,
        delegate: T,
        debug_bridge: UiPathDebugBridge,
    ):
        """Initialize the UiPathDebugRuntime."""
        super().__init__(context)
        self.context: UiPathRuntimeContext = context
        self.delegate: T = delegate
        self.debug_bridge: UiPathDebugBridge = debug_bridge

    async def execute(self) -> UiPathRuntimeResult:
        """Execute the workflow with debug support."""
        try:
            await self.debug_bridge.connect()

            await self.debug_bridge.emit_execution_started()

            result: UiPathRuntimeResult
            # Try to stream events from inner runtime
            try:
                result = await self._stream_and_debug(self.delegate)
            except UiPathStreamNotSupportedError:
                # Fallback to regular execute if streaming not supported
                logger.debug(
                    f"Runtime {self.delegate.__class__.__name__} does not support "
                    "streaming, falling back to execute()"
                )
                result = await self.delegate.execute()

            await self.debug_bridge.emit_execution_completed(result)

            self.context.result = result

            return result

        except Exception as e:
            # Emit execution error
            self.context.result = UiPathRuntimeResult(
                status=UiPathRuntimeStatus.FAULTED,
            )
            await self.debug_bridge.emit_execution_error(
                error=str(e),
            )
            raise

    async def _stream_and_debug(self, inner_runtime: T) -> UiPathRuntimeResult:
        """Stream events from inner runtime and handle debug interactions."""
        final_result: UiPathRuntimeResult
        execution_completed = False

        # Starting in paused state - wait for breakpoints and resume
        await self.debug_bridge.wait_for_resume()

        # Keep streaming until execution completes (not just paused at breakpoint)
        while not execution_completed:
            # Update breakpoints from debug bridge
            inner_runtime.context.breakpoints = self.debug_bridge.get_breakpoints()
            # Stream events from inner runtime
            async for event in inner_runtime.stream():
                # Handle final result
                if isinstance(event, UiPathRuntimeResult):
                    final_result = event

                    # Check if it's a breakpoint result
                    if isinstance(event, UiPathBreakpointResult):
                        try:
                            # Hit a breakpoint - wait for resume and continue
                            await self.debug_bridge.emit_breakpoint_hit(event)
                            await self.debug_bridge.wait_for_resume()

                            self.delegate.context.resume = True

                        except UiPathDebugQuitError:
                            final_result = UiPathRuntimeResult(
                                status=UiPathRuntimeStatus.SUCCESSFUL,
                            )
                            execution_completed = True
                    else:
                        # Normal completion or suspension with dynamic interrupt
                        execution_completed = True
                        # Handle dynamic interrupts if present
                        # In the future, poll for resume trigger completion here, using the debug bridge

                # Handle state update events - send to debug bridge
                elif isinstance(event, UiPathRuntimeStateEvent):
                    await self.debug_bridge.emit_state_update(event)

        return final_result

    async def validate(self) -> None:
        """Validate runtime configuration."""
        await self.delegate.validate()

    async def cleanup(self) -> None:
        """Cleanup runtime resources."""
        try:
            await self.delegate.cleanup()
        finally:
            try:
                await self.debug_bridge.disconnect()
            except Exception as e:
                logger.warning(f"Error disconnecting debug bridge: {e}")
