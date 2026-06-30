"""Resumable runtime protocol and implementation."""

import logging
from collections import Counter
from enum import Enum
from typing import Any, AsyncGenerator

from uipath.core.errors import UiPathPendingTriggerError
from uipath.core.triggers import (
    UIPATH_METADATA_KEY,
    UiPathResumeTrigger,
    UiPathResumeTriggerType,
)

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
        # If resuming, restore trigger from storage
        if options and options.resume:
            # restore trigger from storage
            input = await self._restore_resume_input(input)

        while True:
            # Execute the delegate
            result = await self.delegate.execute(input, options=options)
            # If suspended, create and persist trigger
            suspension_result = await self._handle_suspension(result)

            # check if any trigger may be resumed
            if suspension_result.status != UiPathRuntimeStatus.SUSPENDED or not (
                fired_triggers := await self._get_fired_triggers()
            ):
                return suspension_result

            # Note: when resuming a job, orchestrator deletes all triggers associated with it,
            # thus we can resume the runtime at this point without worrying a trigger may be fired 'twice'
            input = fired_triggers
            if not options:
                options = UiPathExecuteOptions(resume=True)
            else:
                options.resume = True

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
        execution_completed = False
        fired_triggers = None

        while not execution_completed:
            async for event in self.delegate.stream(input, options=options):
                if isinstance(event, UiPathRuntimeResult):
                    final_result = event
                else:
                    yield event

            # If suspended, create and persist trigger
            if final_result:
                suspension_result = await self._handle_suspension(final_result)

                # check if any trigger may be resumed
                if suspension_result.status != UiPathRuntimeStatus.SUSPENDED or not (
                    fired_triggers := await self._get_fired_triggers()
                ):
                    yield suspension_result
                    execution_completed = True

                # Note: when resuming a job, orchestrator deletes all triggers associated with it,
                # thus we can resume the runtime at this point without worrying a trigger may be fired 'twice'
                input = fired_triggers

                if not options:
                    options = UiPathStreamOptions(resume=True)
                else:
                    options.resume = True

    async def _get_fired_triggers(self) -> dict[str, Any] | None:
        """Check stored triggers for any that have already fired.

        Skips external triggers (API, Inbox, Timer) whose payloads only arrive
        asynchronously or through Orchestrator resume and cannot be polled at
        suspend time.

        Returns:
            A resume map of {interrupt_id: resume_data} for fired triggers, or None.
        """
        triggers = await self.storage.get_triggers(self.runtime_id)
        if not triggers:
            return None

        pollable_triggers = [
            t
            for t in triggers
            if t.trigger_type
            not in (
                UiPathResumeTriggerType.API,
                UiPathResumeTriggerType.INBOX,
                UiPathResumeTriggerType.TIMER,
            )
        ]
        return await self._build_resume_map(pollable_triggers, all_triggers=triggers)

    async def _restore_resume_input(
        self,
        input: dict[str, Any] | None,
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
                    await self.storage.delete_triggers(self.runtime_id, triggers)
                else:
                    # Multiple triggers - match by interrupt_id
                    matched_triggers = [
                        trigger for trigger in triggers if trigger.interrupt_id in input
                    ]
                    if matched_triggers:
                        await self.storage.delete_triggers(
                            self.runtime_id, matched_triggers
                        )
                    else:
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
        self,
        pollable_triggers: list[UiPathResumeTrigger],
        all_triggers: list[UiPathResumeTrigger] | None = None,
    ) -> dict[str, Any]:
        """Build resume map from triggers: {interrupt_id: resume_data}.

        Args:
            pollable_triggers: List of triggers to read and map.
            all_triggers: Full trigger set to use when detecting sibling triggers.

        Returns:
            A dict mapping interrupt_id to the trigger's resume data.
        """
        resume_map: dict[str, Any] = {}
        trigger_count_by_interrupt_id = Counter(
            trigger.interrupt_id for trigger in all_triggers or pollable_triggers
        )
        for trigger in pollable_triggers:
            assert trigger.interrupt_id is not None, (
                "Trigger interrupt_id cannot be None"
            )
            if trigger.interrupt_id in resume_map:
                continue

            try:
                data = await self.trigger_manager.read_trigger(trigger)
                if trigger_count_by_interrupt_id[trigger.interrupt_id] > 1:
                    data = self._with_trigger_metadata(trigger, data)
                resume_map[trigger.interrupt_id] = data
                sibling_triggers = [
                    sibling
                    for sibling in all_triggers or pollable_triggers
                    if sibling.interrupt_id == trigger.interrupt_id
                ]
                await self.storage.delete_triggers(self.runtime_id, sibling_triggers)
            except UiPathPendingTriggerError:
                # Trigger still pending, skip it
                pass

        return resume_map

    def _with_trigger_metadata(
        self, trigger: UiPathResumeTrigger, data: Any
    ) -> dict[str, Any]:
        metadata = {
            "triggerType": self._metadata_value(trigger.trigger_type),
            "triggerName": self._metadata_value(trigger.trigger_name),
        }

        if isinstance(data, dict):
            existing_metadata = data.get(UIPATH_METADATA_KEY)
            if isinstance(existing_metadata, dict):
                metadata = {**existing_metadata, **metadata}

            return {**data, UIPATH_METADATA_KEY: metadata}

        return {UIPATH_METADATA_KEY: metadata, "value": data}

    @staticmethod
    def _metadata_value(value: Any) -> Any:
        if isinstance(value, Enum):
            return value.value

        return value

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
            triggers = await self.trigger_manager.create_triggers(
                current_interrupts[interrupt_id]
            )
            for trigger in triggers:
                trigger.interrupt_id = interrupt_id
                suspended_result.triggers.append(trigger)

        if suspended_result.triggers:
            await self.storage.save_triggers(self.runtime_id, suspended_result.triggers)
            # Backward compatibility: set single trigger directly
            suspended_result.trigger = suspended_result.triggers[0]

        return suspended_result

    async def get_schema(self) -> UiPathRuntimeSchema:
        """Passthrough schema from delegate runtime."""
        return await self.delegate.get_schema()

    async def dispose(self) -> None:
        """Cleanup resources for both wrapper and delegate."""
        await self.delegate.dispose()
