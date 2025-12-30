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

    manager.create_trigger = AsyncMock(side_effect=create_trigger_impl)
    manager.read_trigger = AsyncMock()

    return cast(UiPathResumeTriggerProtocol, manager)


@pytest.mark.asyncio
async def test_resumable_creates_multiple_triggers_on_first_suspension():
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
async def test_resumable_adds_only_new_triggers_on_partial_resume():
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
    result2 = await resumable.execute(None, options=UiPathExecuteOptions(resume=True))

    # Should have 2 triggers: int-2 (existing) + int-3 (new)
    assert result2.status == UiPathRuntimeStatus.SUSPENDED
    assert result2.triggers is not None
    assert len(result2.triggers) == 2
    assert {t.interrupt_id for t in result2.triggers} == {"int-2", "int-3"}

    # Only one new trigger created (int-3) - total 3 calls (2 from first + 1 new)
    assert cast(AsyncMock, trigger_manager.create_trigger).await_count == 3


@pytest.mark.asyncio
async def test_resumable_completes_after_all_triggers_resolved():
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
    result = await resumable.execute(None, options=UiPathExecuteOptions(resume=True))

    # Should be successful now
    assert result.status == UiPathRuntimeStatus.SUCCESSFUL
    assert isinstance(result.output, dict)
    assert result.output["completed"] is True
    assert "int-2" in result.output["resume_data"]
    assert "int-3" in result.output["resume_data"]
