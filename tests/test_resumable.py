"""Tests for UiPathResumableRuntime with multiple triggers."""

from __future__ import annotations

from typing import Any, AsyncGenerator, cast
from unittest.mock import AsyncMock, Mock

import pytest
from uipath.core.errors import UiPathPendingTriggerError

from uipath.runtime import (
    UiPathExecuteOptions,
    UiPathResumeTrigger,
    UiPathResumeTriggerType,
    UiPathRuntimeResult,
    UiPathRuntimeStatus,
    UiPathStreamOptions,
)
from uipath.runtime.events import UiPathRuntimeEvent
from uipath.runtime.resumable.protocols import (
    UiPathResumeTriggerProtocol,
)
from uipath.runtime.resumable.runtime import UiPathResumableRuntime
from uipath.runtime.schema import UiPathRuntimeSchema


class MultiTriggerMockRuntime:
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
        self.triggers = [
            t for t in self.triggers if t.interrupt_id != trigger.interrupt_id
        ]

    async def set_value(
        self, runtime_id: str, namespace: str, key: str, value: Any
    ) -> None:
        pass

    async def get_value(self, runtime_id: str, namespace: str, key: str) -> Any:
        return None


def make_trigger_manager_mock() -> UiPathResumeTriggerProtocol:
    """Create trigger manager mock."""
    manager = Mock(spec=UiPathResumeTriggerProtocol)

    def create_trigger_impl(data: dict[str, Any]) -> UiPathResumeTrigger:
        return UiPathResumeTrigger(
            interrupt_id="",  # Will be set by resumable runtime
            trigger_type=UiPathResumeTriggerType.API,
            payload=data,
        )

    async def read_trigger_default(trigger: UiPathResumeTrigger) -> dict[str, Any]:
        # Default behavior: triggers are pending
        raise UiPathPendingTriggerError("Trigger not fired yet")

    manager.create_trigger = AsyncMock(side_effect=create_trigger_impl)
    manager.read_trigger = AsyncMock(side_effect=read_trigger_default)

    return cast(UiPathResumeTriggerProtocol, manager)


class TestResumableRuntime:
    @pytest.mark.asyncio
    async def test_resumable_creates_multiple_triggers_on_first_suspension(
        self,
    ) -> None:
        """First suspension with parallel branches should create multiple triggers."""

        runtime_impl = MultiTriggerMockRuntime()
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

        runtime_impl = MultiTriggerMockRuntime()
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
            raise UiPathPendingTriggerError("still pending")

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

        runtime_impl = MultiTriggerMockRuntime()
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
            raise UiPathPendingTriggerError("pending")

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

        runtime_impl = MultiTriggerMockRuntime()
        storage = StatefulStorageMock()
        trigger_manager = make_trigger_manager_mock()

        # Configure trigger manager: only int-1 is immediately fired, int-2 stays pending
        async def read_trigger_impl(trigger: UiPathResumeTrigger) -> dict[str, Any]:
            # Only int-1 is immediately available
            if trigger.interrupt_id == "int-1":
                return {"approved": True}
            # All other triggers are pending
            raise UiPathPendingTriggerError("pending")

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

        runtime_impl = MultiTriggerMockRuntime()
        storage = StatefulStorageMock()
        trigger_manager = make_trigger_manager_mock()

        # Configure trigger manager so int-1 is fired but int-2 is pending
        async def read_trigger_impl(trigger: UiPathResumeTrigger) -> dict[str, Any]:
            if trigger.interrupt_id == "int-1":
                return {"approved": True}
            raise UiPathPendingTriggerError("still pending")

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

        runtime_impl = MultiTriggerMockRuntime()
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
            raise UiPathPendingTriggerError("pending")

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

        runtime_impl = MultiTriggerMockRuntime()
        storage = StatefulStorageMock()
        trigger_manager = make_trigger_manager_mock()

        # Configure int-1 to be immediately fired, int-2 pending
        async def read_trigger_impl(trigger: UiPathResumeTrigger) -> dict[str, Any]:
            if trigger.interrupt_id == "int-1":
                return {"approved": True}
            raise UiPathPendingTriggerError("pending")

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

        runtime_impl = MultiTriggerMockRuntime()
        storage = StatefulStorageMock()
        trigger_manager = make_trigger_manager_mock()

        # All triggers are pending
        async def read_trigger_impl(trigger: UiPathResumeTrigger) -> dict[str, Any]:
            raise UiPathPendingTriggerError("pending")

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
