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
from uipath.runtime.events import UiPathRuntimeEvent, UiPathRuntimeStateEvent
from uipath.runtime.result import UiPathRuntimeResult, UiPathRuntimeStatus
from uipath.runtime.resumable.polling import TriggerPoller
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
        options = options or UiPathExecuteOptions()
        current_input = input
        current_options = UiPathExecuteOptions(
            resume=options.resume,
            breakpoints=options.breakpoints,
            wait_for_triggers=options.wait_for_triggers,
            trigger_poll_interval=options.trigger_poll_interval,
        )

        while True:
            # If resuming, restore trigger from storage
            if current_options.resume:
                current_input = await self._restore_resume_input(current_input)

            # Execute the delegate
            result = await self.delegate.execute(current_input, options=current_options)

            # If suspended, create trigger (and persist unless wait_for_triggers)
            # When wait_for_triggers=True, we skip storage to avoid persistence
            result = await self._handle_suspension(
                result, skip_storage=options.wait_for_triggers
            )

            # If not suspended or wait_for_triggers is False, return the result
            if (
                result.status != UiPathRuntimeStatus.SUSPENDED
                or not options.wait_for_triggers
            ):
                return result

            # Skip breakpoint results - they should be handled by debug runtime
            if isinstance(result, UiPathBreakpointResult):
                return result

            # Poll triggers until completion (no storage involved)
            if result.triggers:
                logger.info(
                    f"Waiting for {len(result.triggers)} trigger(s) to complete..."
                )
                poller = TriggerPoller(
                    reader=self.trigger_manager,
                    poll_interval=options.trigger_poll_interval,
                )
                resume_data = await poller.poll_all_triggers(result.triggers)

                if resume_data:
                    # Continue execution with resume data
                    # No need to delete from storage since we didn't persist
                    current_input = resume_data
                    current_options.resume = True
                    logger.info("Triggers completed, resuming execution...")
                else:
                    # No data returned, return suspended result
                    return result
            else:
                # No triggers to poll, return suspended result
                return result

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
        options = options or UiPathStreamOptions()
        current_input = input
        current_options = UiPathStreamOptions(
            resume=options.resume,
            breakpoints=options.breakpoints,
            wait_for_triggers=options.wait_for_triggers,
            trigger_poll_interval=options.trigger_poll_interval,
        )
        execution_completed = False

        while not execution_completed:
            # If resuming, restore trigger from storage
            if current_options.resume:
                current_input = await self._restore_resume_input(current_input)

            final_result: UiPathRuntimeResult | None = None
            async for event in self.delegate.stream(
                current_input, options=current_options
            ):
                if isinstance(event, UiPathRuntimeResult):
                    final_result = event
                else:
                    yield event

            if not final_result:
                execution_completed = True
                continue

            # If suspended, create trigger (and persist unless wait_for_triggers)
            # When wait_for_triggers=True, we skip storage to avoid persistence
            final_result = await self._handle_suspension(
                final_result, skip_storage=options.wait_for_triggers
            )

            # If not suspended or wait_for_triggers is False, yield result and exit
            if (
                final_result.status != UiPathRuntimeStatus.SUSPENDED
                or not options.wait_for_triggers
            ):
                yield final_result
                execution_completed = True
                continue

            # Skip breakpoint results - they should be handled by debug runtime
            if isinstance(final_result, UiPathBreakpointResult):
                yield final_result
                execution_completed = True
                continue

            # Poll triggers until completion (no storage involved)
            if final_result.triggers:
                logger.info(
                    f"Waiting for {len(final_result.triggers)} trigger(s) to complete..."
                )

                # Emit a state event to indicate we're polling
                yield UiPathRuntimeStateEvent(
                    node_name="<waiting_for_triggers>",
                    payload={
                        "trigger_count": len(final_result.triggers),
                        "trigger_types": [
                            t.trigger_type.value if t.trigger_type else None
                            for t in final_result.triggers
                        ],
                    },
                )

                poller = TriggerPoller(
                    reader=self.trigger_manager,
                    poll_interval=options.trigger_poll_interval,
                )
                resume_data = await poller.poll_all_triggers(final_result.triggers)

                if resume_data:
                    # Emit state event for resumption
                    # No need to delete from storage since we didn't persist
                    yield UiPathRuntimeStateEvent(
                        node_name="<triggers_completed>",
                        payload={"resumed_triggers": list(resume_data.keys())},
                    )

                    # Continue execution with resume data
                    current_input = resume_data
                    current_options.resume = True
                    logger.info("Triggers completed, resuming execution...")
                else:
                    # No data returned, yield suspended result and exit
                    yield final_result
                    execution_completed = True
            else:
                # No triggers to poll, yield suspended result and exit
                yield final_result
                execution_completed = True

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
        self,
        result: UiPathRuntimeResult,
        skip_storage: bool = False,
    ) -> UiPathRuntimeResult:
        """Create and persist resume trigger if execution was suspended.

        Args:
            result: The execution result to check for suspension
            skip_storage: If True, skip saving triggers to storage (used when
                         wait_for_triggers is enabled to avoid persistence)
        """
        # Only handle suspensions
        if result.status != UiPathRuntimeStatus.SUSPENDED:
            return result

        if isinstance(result, UiPathBreakpointResult):
            return result

        suspended_result = UiPathRuntimeResult(
            status=UiPathRuntimeStatus.SUSPENDED,
            output=result.output,
        )

        assert result.output is None or isinstance(result.output, dict), (
            "Suspended runtime output must be a dict of interrupt IDs to resume data"
        )

        # Get existing triggers and current interrupts
        if not skip_storage:
            suspended_result.triggers = (
                await self.storage.get_triggers(self.runtime_id) or []
            )
        else:
            suspended_result.triggers = []

        current_interrupts = result.output or {}

        # Diff: find new interrupts
        existing_ids = [t.interrupt_id for t in suspended_result.triggers]
        new_ids = [key for key in current_interrupts.keys() if key not in existing_ids]

        # Create triggers only for new interrupts (this starts the actual work)
        for interrupt_id in new_ids:
            trigger = await self.trigger_manager.create_trigger(
                current_interrupts[interrupt_id]
            )
            trigger.interrupt_id = interrupt_id
            suspended_result.triggers.append(trigger)

        # Only persist to storage if not skipping (normal suspend flow)
        if suspended_result.triggers and not skip_storage:
            await self.storage.save_triggers(self.runtime_id, suspended_result.triggers)
            # Backward compatibility: set single trigger directly
            suspended_result.trigger = suspended_result.triggers[0]
        elif suspended_result.triggers:
            # Still set single trigger for backward compat, just don't persist
            suspended_result.trigger = suspended_result.triggers[0]

        return suspended_result

    async def get_schema(self) -> UiPathRuntimeSchema:
        """Passthrough schema from delegate runtime."""
        return await self.delegate.get_schema()

    async def dispose(self) -> None:
        """Cleanup resources for both wrapper and delegate."""
        await self.delegate.dispose()
