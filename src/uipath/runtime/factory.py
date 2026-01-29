"""Protocols for creating UiPath runtime instances."""

from typing import Protocol

from pydantic import BaseModel
from uipath.core.tracing import UiPathTraceSettings

from uipath.runtime.base import (
    UiPathDisposableProtocol,
    UiPathRuntimeProtocol,
)
from uipath.runtime.storage import UiPathRuntimeStorageProtocol


class UiPathRuntimeFactorySettings(BaseModel):
    """Runtime settings for execution behavior."""

    model_config = {"arbitrary_types_allowed": True}  # Needed for Callable

    trace_settings: UiPathTraceSettings | None = None


class UiPathRuntimeFactoryProtocol(
    UiPathDisposableProtocol,
    Protocol,
):
    """Protocol for discovering and creating UiPath runtime instances."""

    async def warmup(self) -> None:
        """Pre-load modules and resources to reduce cold start latency."""
        ...

    def discover_entrypoints(self) -> list[str]:
        """Discover all runtime entrypoints."""
        ...

    async def new_runtime(
        self, entrypoint: str, runtime_id: str, **kwargs
    ) -> UiPathRuntimeProtocol:
        """Create a new runtime instance."""
        ...

    async def get_storage(self) -> UiPathRuntimeStorageProtocol | None:
        """Get the factory storage."""
        ...

    async def get_settings(self) -> UiPathRuntimeFactorySettings | None:
        """Get factory settings."""
        ...
