"""Tests for UiPathResumableRuntime."""

from typing import Any, AsyncGenerator
from unittest.mock import MagicMock

import pytest

from uipath.runtime import UiPathExecuteOptions, UiPathRuntimeEvent
from uipath.runtime.base import UiPathStreamOptions
from uipath.runtime.result import UiPathRuntimeResult, UiPathRuntimeStatus
from uipath.runtime.resumable.runtime import UiPathResumableRuntime
from uipath.runtime.schema import UiPathRuntimeSchema


class MockDelegateRuntime:
    """Mock delegate runtime for testing."""

    def __init__(self):
        self.last_input: dict[str, Any] | None = None

    async def execute(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathExecuteOptions | None = None,
    ) -> UiPathRuntimeResult:
        self.last_input = input
        return UiPathRuntimeResult(
            output={"received_input": input}, status=UiPathRuntimeStatus.SUCCESSFUL
        )

    async def stream(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathStreamOptions | None = None,
    ) -> AsyncGenerator[UiPathRuntimeEvent, None]:
        self.last_input = input
        yield UiPathRuntimeResult(
            output={"received_input": input}, status=UiPathRuntimeStatus.SUCCESSFUL
        )

    async def get_schema(self) -> UiPathRuntimeSchema:
        raise NotImplementedError()

    async def dispose(self) -> None:
        pass


class MockStorage:
    """Mock storage for testing."""

    def __init__(self, trigger: Any = None):
        self._trigger = trigger
        self.saved_trigger: Any = None

    async def save_trigger(self, trigger: Any) -> None:
        self.saved_trigger = trigger

    async def get_latest_trigger(self) -> Any:
        return self._trigger


class MockTriggerManager:
    """Mock trigger manager for testing."""

    def __init__(self, resume_data: dict[str, Any] | None = None):
        self._resume_data = resume_data

    async def create_trigger(self, suspend_value: Any) -> Any:
        raise NotImplementedError()

    async def read_trigger(self, trigger: Any) -> Any | None:
        return self._resume_data


@pytest.mark.asyncio
async def test_restore_resume_input_with_empty_dict_fetches_from_storage():
    """Test that empty dict input triggers fetching from storage on resume."""
    delegate = MockDelegateRuntime()
    stored_trigger = MagicMock()
    storage = MockStorage(trigger=stored_trigger)
    resume_data = {"key": "value_from_storage"}
    trigger_manager = MockTriggerManager(resume_data=resume_data)

    runtime = UiPathResumableRuntime(delegate, storage, trigger_manager)

    options = UiPathExecuteOptions(resume=True)
    result = await runtime.execute(input={}, options=options)

    assert delegate.last_input == resume_data
    assert result.status == UiPathRuntimeStatus.SUCCESSFUL


@pytest.mark.asyncio
async def test_restore_resume_input_with_non_empty_dict_uses_provided_input():
    """Test that non-empty dict input is used directly, not fetched from storage."""
    delegate = MockDelegateRuntime()
    stored_trigger = MagicMock()
    storage = MockStorage(trigger=stored_trigger)
    resume_data = {"key": "value_from_storage"}
    trigger_manager = MockTriggerManager(resume_data=resume_data)

    runtime = UiPathResumableRuntime(delegate, storage, trigger_manager)

    provided_input = {"user_provided": "data"}
    options = UiPathExecuteOptions(resume=True)
    result = await runtime.execute(input=provided_input, options=options)

    assert delegate.last_input == provided_input
    assert result.status == UiPathRuntimeStatus.SUCCESSFUL


@pytest.mark.asyncio
async def test_stream_restore_resume_input_with_empty_dict_fetches_from_storage():
    """Test that empty dict input triggers fetching from storage on resume in stream mode."""
    delegate = MockDelegateRuntime()
    stored_trigger = MagicMock()
    storage = MockStorage(trigger=stored_trigger)
    resume_data = {"key": "value_from_storage"}
    trigger_manager = MockTriggerManager(resume_data=resume_data)

    runtime = UiPathResumableRuntime(delegate, storage, trigger_manager)

    options = UiPathStreamOptions(resume=True)
    async for event in runtime.stream(input={}, options=options):
        if isinstance(event, UiPathRuntimeResult):
            assert event.status == UiPathRuntimeStatus.SUCCESSFUL

    assert delegate.last_input == resume_data
