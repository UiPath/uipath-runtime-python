"""Resumable runtime protocol and implementation."""

import logging
from typing import Any, AsyncGenerator

from uipath.runtime.base import (
    UiPathExecuteOptions,
    UiPathRuntimeProtocol,
    UiPathStreamOptions,
)
from uipath.runtime.debug import UiPathBreakpointResult
from uipath.runtime.events import UiPathRuntimeEvent
from uipath.runtime.result import UiPathRuntimeResult, UiPathRuntimeStatus
from uipath.runtime.resumable.protocols import (
    UiPathResumableStorageProtocol,
    UiPathResumeTriggerProtocol,
)
from uipath.runtime.schema import UiPathRuntimeSchema

logger = logging.getLogger(__name__)


class UiPathResumableRuntime:
    """Generic runtime wrapper that adds resume trigger management to any runtime.

    This class wraps any UiPathRuntimeProtocol implementation and handles:
    - Detecting suspensions in execution results
    - Creating and persisting resume triggers via handler
    - Restoring resume triggers from storage on resume
    - Passing through all other runtime operations unchanged
    """

    def __init__(
        self,
        delegate: UiPathRuntimeProtocol,
        storage: UiPathResumableStorageProtocol,
        trigger_manager: UiPathResumeTriggerProtocol,
    ):
        """Initialize the resumable runtime wrapper.

        Args:
            delegate: The underlying runtime to wrap
            storage: Storage for persisting/retrieving resume triggers
            trigger_manager: Manager for creating and reading resume triggers
        """
        self.delegate = delegate
        self.storage = storage
        self.trigger_manager = trigger_manager

    async def execute(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathExecuteOptions | None = None,
    ) -> UiPathRuntimeResult:
        """Execute with resume trigger handling.

        Args:
            input: Input data for execution
            options: Execution options including resume flag

        Returns:
            Execution result, potentially with resume trigger attached
        """
        # If resuming, restore trigger from storage
        if options and options.resume:
            input = await self._restore_resume_input(input)

        # Execute the delegate
        result = await self.delegate.execute(input, options=options)
        # If suspended, create and persist trigger
        return await self._handle_suspension(result)

    async def stream(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathStreamOptions | None = None,
    ) -> AsyncGenerator[UiPathRuntimeEvent, None]:
        """Stream with resume trigger handling.

        Args:
            input: Input data for execution
            options: Stream options including resume flag

        Yields:
            Runtime events during execution, final event is UiPathRuntimeResult
        """
        # If resuming, restore trigger from storage
        if options and options.resume:
            input = await self._restore_resume_input(input)

        final_result: UiPathRuntimeResult | None = None
        async for event in self.delegate.stream(input, options=options):
            if isinstance(event, UiPathRuntimeResult):
                final_result = event
            else:
                yield event

        # If suspended, create and persist trigger
        if final_result:
            yield await self._handle_suspension(final_result)

    async def _restore_resume_input(
        self, input: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """Restore resume input from storage if not provided.

        Args:
            input: User-provided input (takes precedence)

        Returns:
            Input to use for resume, either provided or from storage
        """
        # If user provided explicit input, use it
        if input is not None:
            return input

        # Otherwise, fetch from storage
        trigger = await self.storage.get_latest_trigger()
        if not trigger:
            return None

        # Read trigger data via trigger_manager
        resume_data = await self.trigger_manager.read_trigger(trigger)

        return resume_data

    async def _handle_suspension(
        self, result: UiPathRuntimeResult
    ) -> UiPathRuntimeResult:
        """Create and persist resume trigger if execution was suspended.

        Args:
            result: The execution result to check for suspension
        """
        # Only handle suspensions
        if result.status != UiPathRuntimeStatus.SUSPENDED:
            return result

        if isinstance(result, UiPathBreakpointResult):
            return result

        # Check if trigger already exists in result
        if result.trigger:
            await self.storage.save_trigger(result.trigger)
            return result

        suspended_result = UiPathRuntimeResult(
            status=UiPathRuntimeStatus.SUSPENDED,
            output=result.output,
        )

        if result.output:
            suspended_result.trigger = await self.trigger_manager.create_trigger(
                result.output
            )

            await self.storage.save_trigger(suspended_result.trigger)

        return suspended_result

    async def get_schema(self) -> UiPathRuntimeSchema:
        """Passthrough schema from delegate runtime."""
        return await self.delegate.get_schema()

    async def dispose(self) -> None:
        """Cleanup resources for both wrapper and delegate."""
        await self.delegate.dispose()
