"""Tests for UiPathResumableRuntime with multiple triggers."""

from __future__ import annotations

from typing import Any, AsyncGenerator, cast
from unittest.mock import AsyncMock, Mock

import pytest
from uipath.core.errors import ErrorCategory, UiPathPendingTriggerError
from uipath.core.triggers import (
    UiPathResumeTrigger,
    UiPathResumeTriggerName,
    UiPathResumeTriggerType,
)

from uipath.runtime import (
    UiPathExecuteOptions,
    UiPathRuntimeResult,
    UiPathRuntimeStatus,
    UiPathStreamOptions,
)
from uipath.runtime.events import UiPathRuntimeEvent
from uipath.runtime.resumable.protocols import (
    UiPathResumableStorageProtocol,
    UiPathResumeTriggerProtocol,
)
from uipath.runtime.resumable.runtime import UiPathResumableRuntime
from uipath.runtime.schema import UiPathRuntimeSchema


class SiblingTriggerMockRuntime:
    """Mock runtime that simulates parallel branching with multiple interrupts."""

    def __init__(self) -> None:
        self.execution_count = 0

    async def dispose(self) -> None:
        pass

    async def execute(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathExecuteOptions | None = None,
    ) -> UiPathRuntimeResult:
        """Simulate parallel branches with progressive suspensions."""
        self.execution_count += 1
        is_resume = options and options.resume

        if self.execution_count == 1:
            # First execution: suspend with 2 parallel interrupts
            return UiPathRuntimeResult(
                status=UiPathRuntimeStatus.SUSPENDED,
                output={
                    "int-1": {"action": "approve_branch_1"},
                    "int-2": {"action": "approve_branch_2"},
                },
            )
        elif self.execution_count == 2:
            # Second execution: int-1 completed, int-2 still pending + new int-3
            # input should contain: {"int-1": {"approved": True}}
            assert is_resume
            assert input is not None
            assert "int-1" in input

            return UiPathRuntimeResult(
                status=UiPathRuntimeStatus.SUSPENDED,
                output={
                    "int-2": {"action": "approve_branch_2"},  # still pending
                    "int-3": {"action": "approve_branch_3"},  # new interrupt
                },
            )
        else:
            # Third execution: all completed
            assert is_resume
            assert input is not None
            assert "int-2" in input
            assert "int-3" in input

            return UiPathRuntimeResult(
                status=UiPathRuntimeStatus.SUCCESSFUL,
                output={"completed": True, "resume_data": input},
            )

    async def stream(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathStreamOptions | None = None,
    ) -> AsyncGenerator[UiPathRuntimeEvent, None]:
        """Stream version of execute."""
        result = await self.execute(input, options)
        yield result

    async def get_schema(self) -> UiPathRuntimeSchema:
        raise NotImplementedError()


class MultiTriggerMockRuntime:
    """Mock runtime that suspends on a multi-trigger interrupt, then suspends again."""

    def __init__(self) -> None:
        self.execution_count = 0

    async def dispose(self) -> None:
        pass

    async def execute(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathExecuteOptions | None = None,
    ) -> UiPathRuntimeResult:
        self.execution_count += 1
        is_resume = options and options.resume

        if self.execution_count == 1:
            return UiPathRuntimeResult(
                status=UiPathRuntimeStatus.SUSPENDED,
                output={"child-1": {"process": "first-child"}},
            )

        assert is_resume
        assert input == {
            "child-1": {
                "completed": True,
                "__uipath": {
                    "triggerType": UiPathResumeTriggerType.JOB.value,
                    "triggerName": "Unknown",
                },
            }
        }
        return UiPathRuntimeResult(
            status=UiPathRuntimeStatus.SUSPENDED,
            output={"child-2": {"process": "second-child"}},
        )

    async def stream(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathStreamOptions | None = None,
    ) -> AsyncGenerator[UiPathRuntimeEvent, None]:
        result = await self.execute(input, options)
        yield result

    async def get_schema(self) -> UiPathRuntimeSchema:
        raise NotImplementedError()


class StatefulStorageMock:
    """Stateful storage mock that tracks triggers."""

    def __init__(self) -> None:
        self.triggers: list[UiPathResumeTrigger] = []

    async def get_triggers(self, runtime_id: str) -> list[UiPathResumeTrigger]:
        return list(self.triggers)

    async def save_triggers(
        self, runtime_id: str, triggers: list[UiPathResumeTrigger]
    ) -> None:
        self.triggers = list(triggers)

    async def delete_trigger(
        self, runtime_id: str, trigger: UiPathResumeTrigger
    ) -> None:
        await self.delete_triggers(runtime_id, [trigger])

    async def delete_triggers(
        self, runtime_id: str, triggers: list[UiPathResumeTrigger]
    ) -> None:
        interrupt_ids = {trigger.interrupt_id for trigger in triggers}
        self.triggers = [
            t for t in self.triggers if t.interrupt_id not in interrupt_ids
        ]

    async def set_value(
        self, runtime_id: str, namespace: str, key: str, value: Any
    ) -> None:
        pass

    async def get_value(self, runtime_id: str, namespace: str, key: str) -> Any:
        return None


class DeprecatedDeleteAliasStorage(UiPathResumableStorageProtocol):
    """Storage that relies on the protocol's deprecated singular delete alias."""

    def __init__(self) -> None:
        self.deleted_triggers: list[UiPathResumeTrigger] = []

    async def get_triggers(self, runtime_id: str) -> list[UiPathResumeTrigger]:
        return []

    async def save_triggers(
        self, runtime_id: str, triggers: list[UiPathResumeTrigger]
    ) -> None:
        pass

    async def delete_triggers(
        self, runtime_id: str, triggers: list[UiPathResumeTrigger]
    ) -> None:
        self.deleted_triggers.extend(triggers)

    async def set_value(
        self, runtime_id: str, namespace: str, key: str, value: Any
    ) -> None:
        pass

    async def get_value(self, runtime_id: str, namespace: str, key: str) -> Any:
        return None


class DefaultBulkDeleteStorage(UiPathResumableStorageProtocol):
    """Storage that relies on the protocol's plural delete default."""

    def __init__(self) -> None:
        self.deleted_triggers: list[UiPathResumeTrigger] = []

    async def get_triggers(self, runtime_id: str) -> list[UiPathResumeTrigger]:
        return []

    async def save_triggers(
        self, runtime_id: str, triggers: list[UiPathResumeTrigger]
    ) -> None:
        pass

    async def delete_trigger(
        self, runtime_id: str, trigger: UiPathResumeTrigger
    ) -> None:
        self.deleted_triggers.append(trigger)

    async def set_value(
        self, runtime_id: str, namespace: str, key: str, value: Any
    ) -> None:
        pass

    async def get_value(self, runtime_id: str, namespace: str, key: str) -> Any:
        return None


class DefaultCreateTriggersManager(UiPathResumeTriggerProtocol):
    """Trigger manager that relies on the protocol's plural create default."""

    def __init__(self) -> None:
        self.created_values: list[Any] = []

    async def create_trigger(self, suspend_value: Any) -> UiPathResumeTrigger:
        self.created_values.append(suspend_value)
        return UiPathResumeTrigger(
            interrupt_id="",
            trigger_type=UiPathResumeTriggerType.TASK,
            payload=suspend_value,
        )

    async def read_trigger(self, trigger: UiPathResumeTrigger) -> Any | None:
        return None


def make_trigger_manager_mock() -> UiPathResumeTriggerProtocol:
    """Create trigger manager mock."""
    manager = Mock(spec=UiPathResumeTriggerProtocol)

    def create_trigger_impl(data: dict[str, Any]) -> UiPathResumeTrigger:
        return UiPathResumeTrigger(
            interrupt_id="",  # Will be set by resumable runtime
            trigger_type=UiPathResumeTriggerType.TASK,
            payload=data,
        )

    async def read_trigger_default(trigger: UiPathResumeTrigger) -> dict[str, Any]:
        raise UiPathPendingTriggerError(ErrorCategory.USER, "Trigger not fired yet")

    manager.create_trigger = AsyncMock(side_effect=create_trigger_impl)

    async def create_triggers_impl(data: dict[str, Any]) -> list[UiPathResumeTrigger]:
        return [await manager.create_trigger(data)]

    manager.create_triggers = AsyncMock(side_effect=create_triggers_impl)
    manager.read_trigger = AsyncMock(side_effect=read_trigger_default)

    return cast(UiPathResumeTriggerProtocol, manager)


class TestResumableRuntime:
    @pytest.mark.asyncio
    async def test_delete_triggers_default_delegates_to_singular_delete(
        self,
    ) -> None:
        """Plural delete compatibility default delegates one trigger at a time."""

        storage = DefaultBulkDeleteStorage()
        triggers = [
            UiPathResumeTrigger(
                interrupt_id="int-1",
                trigger_type=UiPathResumeTriggerType.TASK,
            ),
            UiPathResumeTrigger(
                interrupt_id="int-2",
                trigger_type=UiPathResumeTriggerType.TASK,
            ),
        ]

        await storage.delete_triggers("runtime-1", triggers)

        assert storage.deleted_triggers == triggers

    @pytest.mark.asyncio
    async def test_delete_trigger_alias_delegates_to_delete_triggers(self) -> None:
        """Deprecated singular delete API delegates to the plural API."""

        storage = DeprecatedDeleteAliasStorage()
        trigger = UiPathResumeTrigger(
            interrupt_id="int-1",
            trigger_type=UiPathResumeTriggerType.TASK,
        )

        with pytest.warns(
            DeprecationWarning,
            match="delete_trigger\\(\\) is deprecated; use delete_triggers\\(\\) instead",
        ):
            await storage.delete_trigger("runtime-1", trigger)

        assert storage.deleted_triggers == [trigger]

    @pytest.mark.asyncio
    async def test_create_triggers_default_delegates_to_create_trigger(self) -> None:
        """Plural create compatibility default wraps a single created trigger."""

        manager = DefaultCreateTriggersManager()

        triggers = await manager.create_triggers({"action": "approve"})

        assert len(triggers) == 1
        assert triggers[0].payload == {"action": "approve"}
        assert manager.created_values == [{"action": "approve"}]

    def test_with_trigger_metadata_merges_existing_uipath_metadata(self) -> None:
        """Existing UiPath metadata is preserved when trigger metadata is added."""

        storage = StatefulStorageMock()
        trigger_manager = make_trigger_manager_mock()
        resumable = UiPathResumableRuntime(
            delegate=SiblingTriggerMockRuntime(),
            storage=storage,
            trigger_manager=trigger_manager,
            runtime_id="runtime-1",
        )
        trigger = UiPathResumeTrigger(
            interrupt_id="int-1",
            trigger_type=UiPathResumeTriggerType.TIMER,
            trigger_name=UiPathResumeTriggerName.TIMER,
        )

        result = resumable._with_trigger_metadata(
            trigger,
            {"__uipath": {"kind": "timeout", "timeout": 10}, "value": "done"},
        )

        assert result == {
            "__uipath": {
                "kind": "timeout",
                "timeout": 10,
                "triggerType": UiPathResumeTriggerType.TIMER.value,
                "triggerName": UiPathResumeTriggerName.TIMER.value,
            },
            "value": "done",
        }

    def test_with_trigger_metadata_wraps_non_mapping_data(self) -> None:
        """Non-mapping resume values are wrapped with trigger metadata."""

        storage = StatefulStorageMock()
        trigger_manager = make_trigger_manager_mock()
        resumable = UiPathResumableRuntime(
            delegate=SiblingTriggerMockRuntime(),
            storage=storage,
            trigger_manager=trigger_manager,
            runtime_id="runtime-1",
        )
        trigger = UiPathResumeTrigger(
            interrupt_id="int-1",
            trigger_type=UiPathResumeTriggerType.TASK,
            trigger_name=UiPathResumeTriggerName.TASK,
        )

        result = resumable._with_trigger_metadata(trigger, "approved")

        assert result == {
            "__uipath": {
                "triggerType": UiPathResumeTriggerType.TASK.value,
                "triggerName": UiPathResumeTriggerName.TASK.value,
            },
            "value": "approved",
        }
        assert resumable._metadata_value("CustomName") == "CustomName"

    @pytest.mark.asyncio
    async def test_restore_resume_input_deletes_single_trigger_for_explicit_input(
        self,
    ) -> None:
        """Explicit resume input clears the only stored trigger."""

        storage = StatefulStorageMock()
        storage.triggers = [
            UiPathResumeTrigger(
                interrupt_id="int-1",
                trigger_type=UiPathResumeTriggerType.TASK,
            )
        ]
        resumable = UiPathResumableRuntime(
            delegate=SiblingTriggerMockRuntime(),
            storage=storage,
            trigger_manager=make_trigger_manager_mock(),
            runtime_id="runtime-1",
        )

        result = await resumable._restore_resume_input({"int-1": {"approved": True}})

        assert result == {"int-1": {"approved": True}}
        assert storage.triggers == []

    @pytest.mark.asyncio
    async def test_restore_resume_input_deletes_matching_trigger_for_explicit_input(
        self,
    ) -> None:
        """Explicit resume input clears only matching stored triggers."""

        trigger_1 = UiPathResumeTrigger(
            interrupt_id="int-1",
            trigger_type=UiPathResumeTriggerType.TASK,
        )
        trigger_2 = UiPathResumeTrigger(
            interrupt_id="int-2",
            trigger_type=UiPathResumeTriggerType.TASK,
        )
        storage = StatefulStorageMock()
        storage.triggers = [trigger_1, trigger_2]
        resumable = UiPathResumableRuntime(
            delegate=SiblingTriggerMockRuntime(),
            storage=storage,
            trigger_manager=make_trigger_manager_mock(),
            runtime_id="runtime-1",
        )

        result = await resumable._restore_resume_input({"int-2": {"approved": True}})

        assert result == {"int-2": {"approved": True}}
        assert storage.triggers == [trigger_1]

    @pytest.mark.asyncio
    async def test_build_resume_map_skips_duplicate_pollable_trigger(self) -> None:
        """Duplicate pollable triggers for one interrupt are read once."""

        trigger_1 = UiPathResumeTrigger(
            interrupt_id="int-1",
            trigger_type=UiPathResumeTriggerType.TASK,
        )
        trigger_2 = UiPathResumeTrigger(
            interrupt_id="int-1",
            trigger_type=UiPathResumeTriggerType.JOB,
        )
        storage = StatefulStorageMock()
        storage.triggers = [trigger_1, trigger_2]
        trigger_manager = make_trigger_manager_mock()
        read_trigger_mock = AsyncMock(return_value="approved")
        cast(Any, trigger_manager).read_trigger = read_trigger_mock
        resumable = UiPathResumableRuntime(
            delegate=SiblingTriggerMockRuntime(),
            storage=storage,
            trigger_manager=trigger_manager,
            runtime_id="runtime-1",
        )

        result = await resumable._build_resume_map([trigger_1, trigger_2])

        assert result == {
            "int-1": {
                "__uipath": {
                    "triggerType": UiPathResumeTriggerType.TASK.value,
                    "triggerName": "Unknown",
                },
                "value": "approved",
            }
        }
        read_trigger_mock.assert_awaited_once_with(trigger_1)
        assert storage.triggers == []

    @pytest.mark.asyncio
    async def test_resumable_creates_multiple_triggers_on_first_suspension(
        self,
    ) -> None:
        """First suspension with parallel branches should create multiple triggers."""

        runtime_impl = SiblingTriggerMockRuntime()
        storage = StatefulStorageMock()
        trigger_manager = make_trigger_manager_mock()

        resumable = UiPathResumableRuntime(
            delegate=runtime_impl,
            storage=storage,
            trigger_manager=trigger_manager,
            runtime_id="runtime-1",
        )

        result = await resumable.execute({})

        # Should be suspended with 2 triggers
        assert result.status == UiPathRuntimeStatus.SUSPENDED
        assert result.triggers is not None
        assert len(result.triggers) == 2
        assert {t.interrupt_id for t in result.triggers} == {"int-1", "int-2"}

        # Check payloads by interrupt_id (order should be preserved)
        assert result.triggers[0].interrupt_id == "int-1"
        assert result.triggers[0].payload == {"action": "approve_branch_1"}
        assert result.triggers[1].interrupt_id == "int-2"
        assert result.triggers[1].payload == {"action": "approve_branch_2"}

        # Both triggers should be created and saved
        assert cast(AsyncMock, trigger_manager.create_trigger).await_count == 2
        assert len(storage.triggers) == 2

    @pytest.mark.asyncio
    async def test_resumable_adds_only_new_triggers_on_partial_resume(self) -> None:
        """Partial resume should keep pending trigger and add only new ones."""

        runtime_impl = SiblingTriggerMockRuntime()
        storage = StatefulStorageMock()
        trigger_manager = make_trigger_manager_mock()

        # First execution
        resumable = UiPathResumableRuntime(
            delegate=runtime_impl,
            storage=storage,
            trigger_manager=trigger_manager,
            runtime_id="runtime-1",
        )

        result1 = await resumable.execute({})
        assert result1.triggers is not None
        assert len(result1.triggers) == 2  # int-1, int-2

        # Create async side effect function for read_trigger
        async def read_trigger_impl(trigger: UiPathResumeTrigger) -> dict[str, Any]:
            if trigger.interrupt_id == "int-1":
                return {"approved": True}
            raise UiPathPendingTriggerError(ErrorCategory.USER, "still pending")

        # Replace the mock with new side_effect
        trigger_manager.read_trigger = AsyncMock(side_effect=read_trigger_impl)  # type: ignore

        # Second execution (resume)
        result2 = await resumable.execute(
            None, options=UiPathExecuteOptions(resume=True)
        )

        # Should have 2 triggers: int-2 (existing) + int-3 (new)
        assert result2.status == UiPathRuntimeStatus.SUSPENDED
        assert result2.triggers is not None
        assert len(result2.triggers) == 2
        assert {t.interrupt_id for t in result2.triggers} == {"int-2", "int-3"}

        # Only one new trigger created (int-3) - total 3 calls (2 from first + 1 new)
        assert cast(AsyncMock, trigger_manager.create_trigger).await_count == 3

    @pytest.mark.asyncio
    async def test_resumable_completes_after_all_triggers_resolved(self) -> None:
        """After all triggers resolved, execution should complete successfully."""

        runtime_impl = SiblingTriggerMockRuntime()
        storage = StatefulStorageMock()
        trigger_manager = make_trigger_manager_mock()

        resumable = UiPathResumableRuntime(
            delegate=runtime_impl,
            storage=storage,
            trigger_manager=trigger_manager,
            runtime_id="runtime-1",
        )

        # First execution - creates int-1, int-2
        await resumable.execute({})

        # Create async side effect for second resume
        async def read_trigger_impl_2(trigger: UiPathResumeTrigger) -> dict[str, Any]:
            if trigger.interrupt_id == "int-1":
                return {"approved": True}
            raise UiPathPendingTriggerError(ErrorCategory.USER, "pending")

        trigger_manager.read_trigger = AsyncMock(side_effect=read_trigger_impl_2)  # type: ignore

        # Second execution - int-1 resolved, creates int-3
        await resumable.execute(None, options=UiPathExecuteOptions(resume=True))

        # Create async side effect for final resume
        async def read_trigger_impl_3(trigger: UiPathResumeTrigger) -> dict[str, Any]:
            return {"approved": True}

        trigger_manager.read_trigger = AsyncMock(side_effect=read_trigger_impl_3)  # type: ignore

        # Third execution - int-2 and int-3 both resolved
        result = await resumable.execute(
            None, options=UiPathExecuteOptions(resume=True)
        )

        # Should be successful now
        assert result.status == UiPathRuntimeStatus.SUCCESSFUL
        assert isinstance(result.output, dict)
        assert result.output["completed"] is True
        assert "int-2" in result.output["resume_data"]
        assert "int-3" in result.output["resume_data"]

    @pytest.mark.asyncio
    async def test_resumable_auto_resumes_when_triggers_already_fired(self) -> None:
        """When triggers are already fired during suspension, runtime should auto-resume."""

        runtime_impl = SiblingTriggerMockRuntime()
        storage = StatefulStorageMock()
        trigger_manager = make_trigger_manager_mock()

        # Configure trigger manager: only int-1 is immediately fired, int-2 stays pending
        async def read_trigger_impl(trigger: UiPathResumeTrigger) -> dict[str, Any]:
            # Only int-1 is immediately available
            if trigger.interrupt_id == "int-1":
                return {"approved": True}
            # All other triggers are pending
            raise UiPathPendingTriggerError(ErrorCategory.USER, "pending")

        trigger_manager.read_trigger = AsyncMock(side_effect=read_trigger_impl)  # type: ignore

        resumable = UiPathResumableRuntime(
            delegate=runtime_impl,
            storage=storage,
            trigger_manager=trigger_manager,
            runtime_id="runtime-1",
        )

        # First execution - should suspend with int-1 and int-2, but since int-1 is
        # already fired, it should auto-resume and suspend again with int-2 and int-3
        result = await resumable.execute({})

        # The runtime should have auto-resumed once and suspended again
        assert result.status == UiPathRuntimeStatus.SUSPENDED
        assert result.triggers is not None
        assert len(result.triggers) == 2
        # After auto-resume with int-1, we should be at second suspension with int-2, int-3
        assert {t.interrupt_id for t in result.triggers} == {"int-2", "int-3"}

        # Delegate should have been executed twice (initial + auto-resume)
        assert runtime_impl.execution_count == 2

    @pytest.mark.asyncio
    async def test_resumable_auto_resumes_partial_fired_triggers(self) -> None:
        """When only some triggers are fired during suspension, auto-resume with those."""

        runtime_impl = SiblingTriggerMockRuntime()
        storage = StatefulStorageMock()
        trigger_manager = make_trigger_manager_mock()

        # Configure trigger manager so int-1 is fired but int-2 is pending
        async def read_trigger_impl(trigger: UiPathResumeTrigger) -> dict[str, Any]:
            if trigger.interrupt_id == "int-1":
                return {"approved": True}
            raise UiPathPendingTriggerError(ErrorCategory.USER, "still pending")

        trigger_manager.read_trigger = AsyncMock(side_effect=read_trigger_impl)  # type: ignore

        resumable = UiPathResumableRuntime(
            delegate=runtime_impl,
            storage=storage,
            trigger_manager=trigger_manager,
            runtime_id="runtime-1",
        )

        # First execution - int-1 fires immediately, int-2 stays pending
        # Should auto-resume with int-1 and suspend with int-2, int-3
        result = await resumable.execute({})

        assert result.status == UiPathRuntimeStatus.SUSPENDED
        assert result.triggers is not None
        # After auto-resume with int-1, should have int-2 (still pending) + int-3 (new)
        assert {t.interrupt_id for t in result.triggers} == {"int-2", "int-3"}

        # Verify int-1 was consumed (deleted from storage)
        remaining_triggers = await storage.get_triggers("runtime-1")
        assert all(t.interrupt_id != "int-1" for t in remaining_triggers)

    @pytest.mark.asyncio
    async def test_resumable_auto_resumes_multiple_times(self) -> None:
        """When triggers keep being fired immediately, keep auto-resuming until complete."""

        runtime_impl = SiblingTriggerMockRuntime()
        storage = StatefulStorageMock()
        trigger_manager = make_trigger_manager_mock()

        # Track which phase we're in to fire the right triggers
        checked_triggers = set()

        async def read_trigger_impl(trigger: UiPathResumeTrigger) -> dict[str, Any]:
            # First time we see int-1: fire it immediately
            if trigger.interrupt_id == "int-1" and "int-1" not in checked_triggers:
                checked_triggers.add("int-1")
                return {"approved": True}
            # After int-1 fires, we'll see int-2 and int-3
            # Fire them both immediately
            if trigger.interrupt_id in ["int-2", "int-3"]:
                return {"approved": True}
            raise UiPathPendingTriggerError(ErrorCategory.USER, "pending")

        trigger_manager.read_trigger = AsyncMock(side_effect=read_trigger_impl)  # type: ignore

        resumable = UiPathResumableRuntime(
            delegate=runtime_impl,
            storage=storage,
            trigger_manager=trigger_manager,
            runtime_id="runtime-1",
        )

        # Execute once - should auto-resume through all suspensions
        result = await resumable.execute({})

        # Should complete successfully after auto-resuming twice
        # 1st exec: suspend with int-1, int-2 -> int-1 fires -> auto-resume
        # 2nd exec: suspend with int-2, int-3 -> both fire -> auto-resume
        # 3rd exec: complete
        assert result.status == UiPathRuntimeStatus.SUCCESSFUL
        assert isinstance(result.output, dict)
        assert result.output["completed"] is True

        # Delegate should have been executed 3 times
        assert runtime_impl.execution_count == 3

    @pytest.mark.asyncio
    async def test_resumable_stream_auto_resumes_when_triggers_fired(self) -> None:
        """Stream should auto-resume when triggers are already fired."""

        runtime_impl = SiblingTriggerMockRuntime()
        storage = StatefulStorageMock()
        trigger_manager = make_trigger_manager_mock()

        # Configure int-1 to be immediately fired, int-2 pending
        async def read_trigger_impl(trigger: UiPathResumeTrigger) -> dict[str, Any]:
            if trigger.interrupt_id == "int-1":
                return {"approved": True}
            raise UiPathPendingTriggerError(ErrorCategory.USER, "pending")

        trigger_manager.read_trigger = AsyncMock(side_effect=read_trigger_impl)  # type: ignore

        resumable = UiPathResumableRuntime(
            delegate=runtime_impl,
            storage=storage,
            trigger_manager=trigger_manager,
            runtime_id="runtime-1",
        )

        # Stream should auto-resume and yield final result after auto-resume
        events = []
        async for event in resumable.stream({}):
            events.append(event)

        # Should have received exactly one final result (after auto-resume)
        assert len(events) == 1
        assert isinstance(events[0], UiPathRuntimeResult)
        assert events[0].status == UiPathRuntimeStatus.SUSPENDED

        # Should be at second suspension (after auto-resume with int-1)
        assert events[0].triggers is not None
        assert {t.interrupt_id for t in events[0].triggers} == {"int-2", "int-3"}

    @pytest.mark.asyncio
    async def test_resumable_no_auto_resume_when_all_triggers_pending(self) -> None:
        """When all triggers are pending, should NOT auto-resume."""

        runtime_impl = SiblingTriggerMockRuntime()
        storage = StatefulStorageMock()
        trigger_manager = make_trigger_manager_mock()

        # All triggers are pending
        async def read_trigger_impl(trigger: UiPathResumeTrigger) -> dict[str, Any]:
            raise UiPathPendingTriggerError(ErrorCategory.USER, "pending")

        trigger_manager.read_trigger = AsyncMock(side_effect=read_trigger_impl)  # type: ignore

        resumable = UiPathResumableRuntime(
            delegate=runtime_impl,
            storage=storage,
            trigger_manager=trigger_manager,
            runtime_id="runtime-1",
        )

        # Execute - should suspend
        result = await resumable.execute({})

        assert result.status == UiPathRuntimeStatus.SUSPENDED
        assert result.triggers is not None
        assert {t.interrupt_id for t in result.triggers} == {"int-1", "int-2"}

        # Delegate should have been executed only once)
        assert runtime_impl.execution_count == 1

    @pytest.mark.asyncio
    async def test_resumable_skips_api_triggers_on_auto_resume_check(self) -> None:
        """API triggers should be skipped when checking for auto-resume after suspension."""

        runtime_impl = SiblingTriggerMockRuntime()
        storage = StatefulStorageMock()
        trigger_manager = make_trigger_manager_mock()

        # Create trigger manager that returns API trigger type
        def create_api_trigger(data: dict[str, Any]) -> UiPathResumeTrigger:
            return UiPathResumeTrigger(
                interrupt_id="",  # Will be set by resumable runtime
                trigger_type=UiPathResumeTriggerType.API,
                payload=data,
            )

        trigger_manager.create_trigger = AsyncMock(side_effect=create_api_trigger)  # type: ignore

        async def read_trigger_impl(trigger: UiPathResumeTrigger) -> dict[str, Any]:
            return {"approved": True}

        trigger_manager.read_trigger = AsyncMock(side_effect=read_trigger_impl)  # type: ignore

        resumable = UiPathResumableRuntime(
            delegate=runtime_impl,
            storage=storage,
            trigger_manager=trigger_manager,
            runtime_id="runtime-1",
        )

        # Execute - should suspend and NOT auto-resume because they are API triggers
        result = await resumable.execute({})

        assert result.status == UiPathRuntimeStatus.SUSPENDED
        assert result.triggers is not None
        assert len(result.triggers) == 2
        assert {t.interrupt_id for t in result.triggers} == {"int-1", "int-2"}

        # Verify all triggers are API type
        assert all(
            t.trigger_type == UiPathResumeTriggerType.API for t in result.triggers
        )

        # Delegate should have been executed only once (no auto-resume)
        assert runtime_impl.execution_count == 1

    @pytest.mark.asyncio
    async def test_resumable_skips_inbox_triggers_on_auto_resume_check(self) -> None:
        """Inbox triggers should be skipped when checking for auto-resume after suspension.

        Inbox triggers are async-external (payload delivered via Integration
        Services), so calling read_trigger on them at suspend time would hit a
        404 and fault the run. They should behave like API triggers here.
        """

        runtime_impl = SiblingTriggerMockRuntime()
        storage = StatefulStorageMock()
        trigger_manager = make_trigger_manager_mock()

        def create_inbox_trigger(data: dict[str, Any]) -> UiPathResumeTrigger:
            return UiPathResumeTrigger(
                interrupt_id="",  # Will be set by resumable runtime
                trigger_type=UiPathResumeTriggerType.INBOX,
                payload=data,
            )

        trigger_manager.create_trigger = AsyncMock(side_effect=create_inbox_trigger)  # type: ignore[method-assign]

        # Track whether read_trigger is ever called — it must NOT be, otherwise
        # the filter is broken and we'd hit the payload endpoint prematurely.
        read_trigger_guard = AsyncMock(
            side_effect=AssertionError(
                "read_trigger must not be called for Inbox triggers pre-resume"
            )
        )
        trigger_manager.read_trigger = read_trigger_guard  # type: ignore[method-assign]

        resumable = UiPathResumableRuntime(
            delegate=runtime_impl,
            storage=storage,
            trigger_manager=trigger_manager,
            runtime_id="runtime-1",
        )

        result = await resumable.execute({})

        assert result.status == UiPathRuntimeStatus.SUSPENDED
        assert result.triggers is not None
        assert len(result.triggers) == 2
        assert all(
            t.trigger_type == UiPathResumeTriggerType.INBOX for t in result.triggers
        )
        trigger_manager.read_trigger.assert_not_called()
        assert runtime_impl.execution_count == 1

    @pytest.mark.asyncio
    async def test_resumable_skips_timer_triggers_on_auto_resume_check(self) -> None:
        """Timer triggers should be skipped when checking for auto-resume."""

        runtime_impl = SiblingTriggerMockRuntime()
        storage = StatefulStorageMock()
        trigger_manager = make_trigger_manager_mock()

        def create_timer_trigger(data: dict[str, Any]) -> UiPathResumeTrigger:
            return UiPathResumeTrigger(
                interrupt_id="",  # Will be set by resumable runtime
                trigger_type=UiPathResumeTriggerType.TIMER,
                payload=data,
            )

        trigger_manager.create_trigger = AsyncMock(side_effect=create_timer_trigger)  # type: ignore[method-assign]
        read_trigger_guard = AsyncMock(
            side_effect=AssertionError(
                "read_trigger must not be called for Timer triggers pre-resume"
            )
        )
        trigger_manager.read_trigger = read_trigger_guard  # type: ignore[method-assign]

        resumable = UiPathResumableRuntime(
            delegate=runtime_impl,
            storage=storage,
            trigger_manager=trigger_manager,
            runtime_id="runtime-1",
        )

        result = await resumable.execute({})

        assert result.status == UiPathRuntimeStatus.SUSPENDED
        assert result.triggers is not None
        assert len(result.triggers) == 2
        assert all(
            t.trigger_type == UiPathResumeTriggerType.TIMER for t in result.triggers
        )
        trigger_manager.read_trigger.assert_not_called()
        assert runtime_impl.execution_count == 1

    @pytest.mark.asyncio
    async def test_resumable_auto_resumes_task_triggers_but_not_api_triggers(
        self,
    ) -> None:
        """Mixed triggers: TASK triggers should trigger auto-resume, API triggers should not."""

        runtime_impl = SiblingTriggerMockRuntime()
        storage = StatefulStorageMock()
        trigger_manager = make_trigger_manager_mock()

        # Create different trigger types: int-1 is TASK, int-2 is API
        def create_typed_trigger(data: dict[str, Any]) -> UiPathResumeTrigger:
            # Determine trigger type based on payload action
            if "approve_branch_1" in str(data):
                trigger_type = UiPathResumeTriggerType.TASK
            else:
                trigger_type = UiPathResumeTriggerType.API

            return UiPathResumeTrigger(
                interrupt_id="",  # Will be set by resumable runtime
                trigger_type=trigger_type,
                payload=data,
            )

        trigger_manager.create_trigger = AsyncMock(side_effect=create_typed_trigger)  # type: ignore

        # only TASK should trigger auto-resume
        async def read_trigger_impl(trigger: UiPathResumeTrigger) -> dict[str, Any]:
            return {"approved": True}

        trigger_manager.read_trigger = AsyncMock(side_effect=read_trigger_impl)  # type: ignore

        resumable = UiPathResumableRuntime(
            delegate=runtime_impl,
            storage=storage,
            trigger_manager=trigger_manager,
            runtime_id="runtime-1",
        )

        # Execute - should auto-resume based on int-1 (TASK) but skip int-2 (API)
        result = await resumable.execute({})

        # Should have auto-resumed once (because of TASK trigger)
        assert result.status == UiPathRuntimeStatus.SUSPENDED
        assert result.triggers is not None

        # After auto-resume with int-1, should be at second suspension with int-2 and int-3
        assert {t.interrupt_id for t in result.triggers} == {"int-2", "int-3"}

        # Delegate should have been executed twice (initial + auto-resume for TASK trigger)
        assert runtime_impl.execution_count == 2

    @pytest.mark.asyncio
    async def test_resumable_removes_sibling_triggers_when_one_trigger_fires(
        self,
    ) -> None:
        """When one trigger fires, sibling triggers must not leak forward."""

        runtime_impl = MultiTriggerMockRuntime()
        storage = StatefulStorageMock()
        trigger_manager = make_trigger_manager_mock()

        async def create_sibling_triggers(
            data: dict[str, Any],
        ) -> list[UiPathResumeTrigger]:
            return [
                UiPathResumeTrigger(
                    interrupt_id="",  # Will be set by resumable runtime
                    trigger_type=UiPathResumeTriggerType.JOB,
                    payload={"source": data["process"]},
                ),
                UiPathResumeTrigger(
                    interrupt_id="",  # Will be set by resumable runtime
                    trigger_type=UiPathResumeTriggerType.TIMER,
                    payload={"source": data["process"]},
                ),
            ]

        create_triggers_mock = AsyncMock(side_effect=create_sibling_triggers)
        cast(Any, trigger_manager).create_triggers = create_triggers_mock

        async def read_trigger_impl(trigger: UiPathResumeTrigger) -> dict[str, Any]:
            if (
                trigger.interrupt_id == "child-1"
                and trigger.trigger_type == UiPathResumeTriggerType.JOB
            ):
                return {"completed": True}
            raise UiPathPendingTriggerError(ErrorCategory.USER, "still pending")

        read_trigger_mock = AsyncMock(side_effect=read_trigger_impl)
        cast(Any, trigger_manager).read_trigger = read_trigger_mock

        resumable = UiPathResumableRuntime(
            delegate=runtime_impl,
            storage=storage,
            trigger_manager=trigger_manager,
            runtime_id="runtime-1",
        )

        result = await resumable.execute({})

        assert result.status == UiPathRuntimeStatus.SUSPENDED
        assert result.triggers is not None
        assert {t.interrupt_id for t in result.triggers} == {"child-2"}
        assert len(result.triggers) == 2
        assert {t.trigger_type for t in result.triggers} == {
            UiPathResumeTriggerType.JOB,
            UiPathResumeTriggerType.TIMER,
        }
        assert runtime_impl.execution_count == 2
        assert create_triggers_mock.await_count == 2
        assert read_trigger_mock.await_count == 2

        saved_triggers = await storage.get_triggers("runtime-1")
        assert {t.interrupt_id for t in saved_triggers} == {"child-2"}
