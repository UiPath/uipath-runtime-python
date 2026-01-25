from typing import Any, AsyncGenerator

import pytest

from uipath.runtime import (
    UiPathExecuteOptions,
    UiPathRuntimeEvent,
    UiPathRuntimeProtocol,
    UiPathRuntimeResult,
    UiPathRuntimeSchema,
    UiPathRuntimeStorageProtocol,
    UiPathStreamOptions,
)
from uipath.runtime.factory import (
    UiPathRuntimeFactoryProtocol,
    UiPathRuntimeFactorySettings,
)


class MockStorage(UiPathRuntimeStorageProtocol):
    """Mock storage implementation"""

    def __init__(self):
        self._store = {}

    async def set_value(self, runtime_id, namespace, key, value):
        self._store.setdefault(runtime_id, {}).setdefault(namespace, {})[key] = value

    async def get_value(self, runtime_id, namespace, key):
        return self._store.get(runtime_id, {}).get(namespace, {}).get(key)


class MockRuntime(UiPathRuntimeProtocol):
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

    def discover_entrypoints(self) -> list[str]:
        return ["main.py"]

    async def new_runtime(
        self, entrypoint: str, runtime_id: str, **kwargs
    ) -> UiPathRuntimeProtocol:
        return MockRuntime(kwargs.get("settings"))

    async def get_storage(self) -> UiPathRuntimeStorageProtocol | None:
        return MockStorage()

    async def get_settings(self) -> UiPathRuntimeFactorySettings | None:
        return UiPathRuntimeFactorySettings()

    async def dispose(self) -> None:
        pass


@pytest.mark.asyncio
async def test_protocol_works_with_kwargs_not_specified():
    """Test protocol works with implementation that has kwargs."""
    creator: UiPathRuntimeFactoryProtocol = CreatorWithKwargs()
    runtime = await creator.new_runtime("main.py", "runtime-123")
    assert isinstance(runtime, MockRuntime)


@pytest.mark.asyncio
async def test_protocol_works_with_kwargs_specified():
    """Test protocol works with implementation that has kwargs."""
    creator: UiPathRuntimeFactoryProtocol = CreatorWithKwargs()
    runtime = await creator.new_runtime(
        "main.py", "runtime-123", settings={"timeout": 30, "model": "gpt-4"}
    )
    assert isinstance(runtime, MockRuntime)
    assert runtime.settings == {"timeout": 30, "model": "gpt-4"}
