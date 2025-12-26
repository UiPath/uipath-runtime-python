"""Debug runtime implementation."""

import asyncio
import logging
from typing import Any, AsyncGenerator, cast

from uipath.core.errors import UiPathPendingTriggerError

from uipath.runtime.base import (
    UiPathExecuteOptions,
    UiPathRuntimeProtocol,
    UiPathStreamNotSupportedError,
    UiPathStreamOptions,
)
from uipath.runtime.debug import (
    UiPathBreakpointResult,
    UiPathDebugProtocol,
    UiPathDebugQuitError,
)
from uipath.runtime.events import (
    UiPathRuntimeEvent,
    UiPathRuntimeStateEvent,
)
from uipath.runtime.result import (
    UiPathRuntimeResult,
    UiPathRuntimeStatus,
)
from uipath.runtime.resumable.protocols import UiPathResumeTriggerReaderProtocol
from uipath.runtime.resumable.runtime import UiPathResumableRuntime
from uipath.runtime.resumable.trigger import (
    UiPathResumeTrigger,
    UiPathResumeTriggerType,
)
from uipath.runtime.schema import UiPathRuntimeSchema

logger = logging.getLogger(__name__)


class UiPathDebugRuntime:
    """Specialized runtime for debug runs that streams events to a debug bridge."""

    def __init__(
        self,
        delegate: UiPathRuntimeProtocol,
        debug_bridge: UiPathDebugProtocol,
        trigger_poll_interval: float = 5.0,
    ):
        """Initialize the UiPathDebugRuntime.

        Args:
            delegate: The underlying runtime to wrap
            debug_bridge: Bridge for debug event communication
            trigger_poll_interval: Seconds between poll attempts for resume triggers (default: 5.0, disabled: 0.0)
        """
        super().__init__()
        self.delegate = delegate
        self.debug_bridge: UiPathDebugProtocol = debug_bridge
        if trigger_poll_interval < 0:
            raise ValueError("trigger_poll_interval must be >= 0")
        self.trigger_poll_interval = trigger_poll_interval

    async def execute(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathExecuteOptions | None = None,
    ) -> UiPathRuntimeResult:
        """Execute the workflow with debug support."""
        final_result = None
        async for event in self.stream(input, cast(UiPathStreamOptions, options)):
            if isinstance(event, UiPathRuntimeResult):
                final_result = event

        return (
            final_result
            if final_result
            else UiPathRuntimeResult(status=UiPathRuntimeStatus.SUCCESSFUL)
        )

    async def stream(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathStreamOptions | None = None,
    ) -> AsyncGenerator[UiPathRuntimeEvent, None]:
        """Stream execution events with debug support."""
        try:
            await self.debug_bridge.connect()
            await self.debug_bridge.emit_execution_started()

            result: UiPathRuntimeResult | None = None

            # Try to stream events from inner runtime
            try:
                async for event in self._stream_and_debug(input, options=options):
                    yield event
                    if isinstance(event, UiPathRuntimeResult):
                        result = event
            except UiPathStreamNotSupportedError:
                # Fallback to regular execute if streaming not supported
                logger.debug(
                    f"Runtime {self.delegate.__class__.__name__} does not support "
                    "streaming, falling back to execute()"
                )
                result = await self.delegate.execute(input, options=options)
                yield result

            if result:
                await self.debug_bridge.emit_execution_completed(result)

        except Exception as e:
            await self.debug_bridge.emit_execution_error(error=str(e))
            raise

    async def _stream_and_debug(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathExecuteOptions | None = None,
    ) -> AsyncGenerator[UiPathRuntimeEvent, None]:
        """Stream events from inner runtime and handle debug interactions."""
        final_result: UiPathRuntimeResult
        execution_completed = False

        # Starting in paused state - wait for breakpoints and resume
        try:
            await asyncio.wait_for(self.debug_bridge.wait_for_resume(), timeout=60.0)
        except asyncio.TimeoutError:
            logger.warning(
                "Initial resume wait timed out after 60s, assuming debug bridge disconnected"
            )
            yield UiPathRuntimeResult(status=UiPathRuntimeStatus.FAULTED)
            return
        except UiPathDebugQuitError:
            logger.info("Debug session quit by user before execution started")
            yield UiPathRuntimeResult(status=UiPathRuntimeStatus.SUCCESSFUL)
            return

        debug_options = UiPathStreamOptions(
            resume=options.resume if options else False,
            breakpoints=options.breakpoints if options else None,
        )

        current_input = input

        # Keep streaming until execution completes (not just paused at breakpoint)
        while not execution_completed:
            # Update breakpoints from debug bridge
            debug_options.breakpoints = self.debug_bridge.get_breakpoints()

            # Stream events from inner runtime
            async for event in self.delegate.stream(
                current_input, options=debug_options
            ):
                yield event

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
                            current_input = (
                                None  # Resume with no new input (very important)
                            )

                        except UiPathDebugQuitError:
                            final_result = UiPathRuntimeResult(
                                status=UiPathRuntimeStatus.SUCCESSFUL,
                            )
                            yield final_result
                            execution_completed = True
                    else:
                        # Normal completion or suspension with dynamic interrupt

                        # Check if this is a suspended execution that needs polling
                        if (
                            isinstance(self.delegate, UiPathResumableRuntime)
                            and self.trigger_poll_interval > 0
                            and final_result.status == UiPathRuntimeStatus.SUSPENDED
                            and final_result.trigger
                        ):
                            await self.debug_bridge.emit_execution_suspended(
                                final_result
                            )

                            interrupt_id = final_result.trigger.interrupt_id
                            assert interrupt_id is not None

                            resume_data: dict[str, Any] | None = None
                            try:
                                trigger_data: dict[str, Any] | None = None
                                if (
                                    final_result.trigger.trigger_type
                                    == UiPathResumeTriggerType.API
                                ):
                                    trigger_data = (
                                        await self.debug_bridge.wait_for_resume()
                                    )
                                else:
                                    trigger_data = await self._poll_trigger(
                                        final_result.trigger,
                                        self.delegate.trigger_manager,
                                    )
                                resume_data = {interrupt_id: trigger_data}
                            except UiPathDebugQuitError:
                                final_result = UiPathRuntimeResult(
                                    status=UiPathRuntimeStatus.SUCCESSFUL,
                                )
                                yield final_result
                                execution_completed = True

                            if resume_data is not None:
                                await self.debug_bridge.emit_execution_resumed(
                                    resume_data
                                )

                                # Continue with resumed execution
                                current_input = resume_data
                                debug_options.resume = True
                                # Don't mark as completed - continue the loop
                            else:
                                execution_completed = True
                        else:
                            # Normal completion - mark as done
                            execution_completed = True

                # Handle state update events - send to debug bridge
                elif isinstance(event, UiPathRuntimeStateEvent):
                    await self.debug_bridge.emit_state_update(event)

    async def get_schema(self) -> UiPathRuntimeSchema:
        """Passthrough schema for the delegate."""
        return await self.delegate.get_schema()

    async def dispose(self) -> None:
        """Cleanup runtime resources."""
        try:
            await self.debug_bridge.disconnect()
        except Exception as e:
            logger.warning(f"Error disconnecting debug bridge: {e}")

    async def _poll_trigger(
        self, trigger: UiPathResumeTrigger, reader: UiPathResumeTriggerReaderProtocol
    ) -> dict[str, Any] | None:
        """Poll a resume trigger until data is available.

        Args:
            trigger: The trigger to poll
            reader: The trigger reader to use for polling

        Returns:
            Resume data when available, or None if polling exhausted

        Raises:
            UiPathDebugQuitError: If quit is requested during polling
        """
        attempt = 0
        while True:
            attempt += 1

            try:
                resume_data = await reader.read_trigger(trigger)

                if resume_data is not None:
                    return resume_data

                await self.debug_bridge.emit_state_update(
                    UiPathRuntimeStateEvent(
                        node_name="<polling>",
                        payload={
                            "attempt": attempt,
                        },
                    )
                )

                await self._wait_with_quit_check()

            except UiPathDebugQuitError:
                raise

            except UiPathPendingTriggerError as e:
                await self.debug_bridge.emit_state_update(
                    UiPathRuntimeStateEvent(
                        node_name="<polling>",
                        payload={
                            "attempt": attempt,
                            "info": str(e),
                        },
                    )
                )

                await self._wait_with_quit_check()

    async def _wait_with_quit_check(self) -> None:
        """Wait for specified seconds, but allow quit command to interrupt.

        Raises:
            UiPathDebugQuitError: If quit is requested during wait
        """
        sleep_task = asyncio.create_task(asyncio.sleep(self.trigger_poll_interval))
        term_task = asyncio.create_task(self.debug_bridge.wait_for_terminate())

        done, pending = await asyncio.wait(
            {sleep_task, term_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        if term_task in done:
            raise UiPathDebugQuitError("Debugging terminated during polling.")
