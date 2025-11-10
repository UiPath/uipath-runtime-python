"""Debug runtime implementation."""

import logging
from typing import Any, Optional

from uipath.runtime import (
    UiPathBreakpointResult,
    UiPathExecuteOptions,
    UiPathRuntimeProtocol,
    UiPathRuntimeResult,
    UiPathRuntimeStatus,
    UiPathStreamNotSupportedError,
    UiPathStreamOptions,
)
from uipath.runtime.debug import UiPathDebugBridgeProtocol, UiPathDebugQuitError
from uipath.runtime.events import (
    UiPathRuntimeStateEvent,
)
from uipath.runtime.schema import UiPathRuntimeSchema

logger = logging.getLogger(__name__)


class UiPathDebugRuntime:
    """Specialized runtime for debug runs that streams events to a debug bridge."""

    def __init__(
        self,
        delegate: UiPathRuntimeProtocol,
        debug_bridge: UiPathDebugBridgeProtocol,
    ):
        """Initialize the UiPathDebugRuntime."""
        super().__init__()
        self.delegate = delegate
        self.debug_bridge: UiPathDebugBridgeProtocol = debug_bridge

    async def execute(
        self,
        input: Optional[dict[str, Any]] = None,
        options: Optional[UiPathExecuteOptions] = None,
    ) -> UiPathRuntimeResult:
        """Execute the workflow with debug support."""
        try:
            await self.debug_bridge.connect()

            await self.debug_bridge.emit_execution_started()

            result: UiPathRuntimeResult

            # Try to stream events from inner runtime
            try:
                result = await self._stream_and_debug(input, options=options)
            except UiPathStreamNotSupportedError:
                # Fallback to regular execute if streaming not supported
                logger.debug(
                    f"Runtime {self.delegate.__class__.__name__} does not support "
                    "streaming, falling back to execute()"
                )
                result = await self.delegate.execute(input, options=options)

            await self.debug_bridge.emit_execution_completed(result)

            return result

        except Exception as e:
            await self.debug_bridge.emit_execution_error(
                error=str(e),
            )
            raise

    async def _stream_and_debug(
        self,
        input: Optional[dict[str, Any]] = None,
        options: Optional[UiPathExecuteOptions] = None,
    ) -> UiPathRuntimeResult:
        """Stream events from inner runtime and handle debug interactions."""
        final_result: UiPathRuntimeResult
        execution_completed = False

        # Starting in paused state - wait for breakpoints and resume
        await self.debug_bridge.wait_for_resume()

        debug_options = UiPathStreamOptions(
            resume=options.resume if options else False,
            breakpoints=options.breakpoints if options else None,
        )

        # Keep streaming until execution completes (not just paused at breakpoint)
        while not execution_completed:
            # Update breakpoints from debug bridge
            debug_options.breakpoints = self.debug_bridge.get_breakpoints()
            # Stream events from inner runtime
            async for event in self.delegate.stream(input, options=debug_options):
                # Handle final result
                if isinstance(event, UiPathRuntimeResult):
                    final_result = event

                    # Check if it's a breakpoint result
                    if isinstance(event, UiPathBreakpointResult):
                        try:
                            # Hit a breakpoint - wait for resume and continue
                            await self.debug_bridge.emit_breakpoint_hit(event)
                            await self.debug_bridge.wait_for_resume()

                            # Tell inner runtime we're resuming
                            debug_options.resume = True

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

    async def get_schema(self) -> UiPathRuntimeSchema:
        """Passthrough schema for the delegate."""
        return await self.delegate.get_schema()

    async def dispose(self) -> None:
        """Cleanup runtime resources."""
        try:
            await self.debug_bridge.disconnect()
        except Exception as e:
            logger.warning(f"Error disconnecting debug bridge: {e}")
