"""Resumable runtime protocol and implementation."""

import logging
from typing import Any, AsyncGenerator

from uipath.core.errors import UiPathPendingTriggerError

from uipath.runtime.base import (
    UiPathExecuteOptions,
    UiPathRuntimeProtocol,
    UiPathStreamOptions,
)
from uipath.runtime.debug.breakpoint import UiPathBreakpointResult
from uipath.runtime.events import UiPathRuntimeEvent
from uipath.runtime.result import (
    UiPathRuntimeResult,
    UiPathRuntimeStatus,
)
from uipath.runtime.resumable.protocols import (
    UiPathResumableStorageProtocol,
    UiPathResumeTriggerProtocol,
)
from uipath.runtime.resumable.trigger import UiPathResumeTrigger
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
        runtime_id: str,
    ):
        """Initialize the resumable runtime wrapper.

        Args:
            delegate: The underlying runtime to wrap
            storage: Storage for persisting/retrieving resume triggers
            trigger_manager: Manager for creating and reading resume triggers
            runtime_id: Id used for runtime orchestration
        """
        self.delegate = delegate
        self.storage = storage
        self.trigger_manager = trigger_manager
        self.runtime_id = runtime_id
        self._fired_triggers_map: dict[str, Any] = {}

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
        # check if we are resuming
        if options and options.resume:
            if self._fired_triggers_map:
                input = self._fired_triggers_map
                self._fired_triggers_map = {}
            else:
                # restore trigger from storage
                input = await self._restore_resume_input(input)

        # Execute the delegate
        result = await self.delegate.execute(input, options=options)
        # If suspended, create and persist trigger
        suspension_result = await self._handle_suspension(result)
        if not self._fired_triggers_map:
            return suspension_result

        # some triggers are already fired, runtime can be resumed
        resume_options = options or UiPathExecuteOptions(resume=True)
        if not resume_options.resume:
            resume_options = UiPathExecuteOptions(resume=True)
        return await self.execute(
            options=resume_options,
        )

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
        # check if we are resuming
        if options and options.resume:
            if self._fired_triggers_map:
                input = self._fired_triggers_map
                self._fired_triggers_map = {}
            else:
                # restore trigger from storage
                input = await self._restore_resume_input(input)

        final_result: UiPathRuntimeResult | None = None
        async for event in self.delegate.stream(input, options=options):
            if isinstance(event, UiPathRuntimeResult):
                final_result = event
            else:
                yield event

        # If suspended, create and persist trigger
        if final_result:
            suspension_result = await self._handle_suspension(final_result)

            if not self._fired_triggers_map:
                yield suspension_result
                return

            # some triggers are already fired, runtime can be resumed
            resume_options = options or UiPathStreamOptions(resume=True)
            if not resume_options.resume:
                resume_options = UiPathStreamOptions(resume=True)
            async for event in self.stream(
                options=resume_options,
            ):
                yield event

    async def _restore_resume_input(
        self, input: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """Restore resume input from storage if not provided.

        Args:
            input: User-provided input (takes precedence)

        Returns:
            Input to use for resume: {interrupt_id: resume_data, ...}
        """
        # Fetch all triggers from storage
        triggers = await self.storage.get_triggers(self.runtime_id)

        # If user provided explicit input, use it
        if input is not None:
            if triggers:
                if len(triggers) == 1:
                    # Single trigger - just delete it
                    await self.storage.delete_trigger(self.runtime_id, triggers[0])
                else:
                    # Multiple triggers - match by interrupt_id
                    found = False
                    for trigger in triggers:
                        if trigger.interrupt_id in input:
                            await self.storage.delete_trigger(self.runtime_id, trigger)
                            found = True
                    if not found:
                        logger.warning(
                            f"Multiple triggers detected but none match the provided input. "
                            f"Please specify which trigger to resume by {{interrupt_id: value}}. "
                            f"Available interrupt_ids: {[t.interrupt_id for t in triggers]}."
                        )
            return input

        if not triggers:
            return None

        return await self._build_resume_map(triggers)

    async def _build_resume_map(
        self, triggers: list[UiPathResumeTrigger]
    ) -> dict[str, Any]:
        # Build resume map: {interrupt_id: resume_data}
        resume_map: dict[str, Any] = {}
        for trigger in triggers:
            try:
                data = await self.trigger_manager.read_trigger(trigger)
                assert trigger.interrupt_id is not None, (
                    "Trigger interrupt_id cannot be None"
                )
                resume_map[trigger.interrupt_id] = data
                await self.storage.delete_trigger(self.runtime_id, trigger)
            except UiPathPendingTriggerError:
                # Trigger still pending, skip it
                pass

        return resume_map

    async def _handle_suspension(
        self, result: UiPathRuntimeResult
    ) -> UiPathRuntimeResult:
        """Create and persist resume trigger if execution was suspended.

        Args:
            result: The execution result to check for suspension
        """
        # Only handle interrupt suspensions
        if result.status != UiPathRuntimeStatus.SUSPENDED or isinstance(
            result, UiPathBreakpointResult
        ):
            return result

        suspended_result = UiPathRuntimeResult(
            status=UiPathRuntimeStatus.SUSPENDED,
            output=result.output,
        )

        assert result.output is None or isinstance(result.output, dict), (
            "Suspended runtime output must be a dict of interrupt IDs to resume data"
        )

        # Get existing triggers and current interrupts
        suspended_result.triggers = (
            await self.storage.get_triggers(self.runtime_id) or []
        )
        current_interrupts = result.output or {}

        # Diff: find new interrupts
        existing_ids = [t.interrupt_id for t in suspended_result.triggers]
        new_ids = [key for key in current_interrupts.keys() if key not in existing_ids]

        # Create triggers only for new interrupts
        for interrupt_id in new_ids:
            trigger = await self.trigger_manager.create_trigger(
                current_interrupts[interrupt_id]
            )
            trigger.interrupt_id = interrupt_id
            suspended_result.triggers.append(trigger)

        if suspended_result.triggers:
            await self.storage.save_triggers(self.runtime_id, suspended_result.triggers)
            # Backward compatibility: set single trigger directly
            suspended_result.trigger = suspended_result.triggers[0]

        # check if any trigger can be resumed
        # Note: when resuming a job, orchestrator deletes all triggers associated with it,
        # thus we can resume the runtime at this point without worrying a trigger may be fired 'twice'
        triggers = await self.storage.get_triggers(self.runtime_id)
        if triggers:
            self._fired_triggers_map = await self._build_resume_map(triggers)

        return suspended_result

    async def get_schema(self) -> UiPathRuntimeSchema:
        """Passthrough schema from delegate runtime."""
        return await self.delegate.get_schema()

    async def dispose(self) -> None:
        """Cleanup resources for both wrapper and delegate."""
        await self.delegate.dispose()
