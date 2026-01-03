from typing import Any, AsyncGenerator

import pytest

from uipath.runtime import (
    UiPathExecuteOptions,
    UiPathRuntimeEvent,
    UiPathRuntimeProtocol,
    UiPathRuntimeResult,
    UiPathRuntimeSchema,
    UiPathStreamOptions,
)
from uipath.runtime.factory import UiPathRuntimeCreatorProtocol


class MockRuntime:
    """Mock runtime that implements UiPathRuntimeProtocol."""

    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        self.settings = settings

    async def execute(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathExecuteOptions | None = None,
    ) -> UiPathRuntimeResult:
        return UiPathRuntimeResult(output={})

    async def stream(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathStreamOptions | None = None,
    ) -> AsyncGenerator[UiPathRuntimeEvent, None]:
        yield UiPathRuntimeResult(output={})

    async def get_schema(self) -> UiPathRuntimeSchema:
        return UiPathRuntimeSchema(
            filePath="agent.json",
            type="agent",
            uniqueId="unique-id",
            input={},
            output={},
        )

    async def dispose(self) -> None:
        pass


class CreatorWithKwargs:
    """Implementation with kwargs."""

    async def new_runtime(
        self, entrypoint: str, runtime_id: str, **kwargs
    ) -> UiPathRuntimeProtocol:
        return MockRuntime(kwargs.get("settings"))


@pytest.mark.asyncio
async def test_protocol_works_with_kwargs_not_specified():
    """Test protocol works with implementation that has kwargs."""
    creator: UiPathRuntimeCreatorProtocol = CreatorWithKwargs()
    runtime = await creator.new_runtime("main.py", "runtime-123")
    assert isinstance(runtime, MockRuntime)


@pytest.mark.asyncio
async def test_protocol_works_with_kwargs_specified():
    """Test protocol works with implementation that has kwargs."""
    creator: UiPathRuntimeCreatorProtocol = CreatorWithKwargs()
    runtime = await creator.new_runtime(
        "main.py", "runtime-123", settings={"timeout": 30, "model": "gpt-4"}
    )
    assert isinstance(runtime, MockRuntime)
    assert runtime.settings == {"timeout": 30, "model": "gpt-4"}
