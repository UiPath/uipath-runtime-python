"""Protocols for creating UiPath runtime instances."""

from typing import Protocol

from uipath.runtime.base import UiPathDisposableProtocol, UiPathRuntimeProtocol


class UiPathRuntimeScannerProtocol(Protocol):
    """Protocol for discovering all UiPath runtime instances."""

    async def discover_runtimes(self) -> list[UiPathRuntimeProtocol]:
        """Discover all runtime classes."""
        ...

    def discover_entrypoints(self) -> list[str]:
        """Discover all runtime entrypoints."""
        ...


class UiPathRuntimeCreatorProtocol(Protocol):
    """Protocol for creating a UiPath runtime given an entrypoint."""

    async def new_runtime(
        self, entrypoint: str, runtime_id: str
    ) -> UiPathRuntimeProtocol:
        """Create a new runtime instance."""
        ...


class UiPathRuntimeFactoryProtocol(
    UiPathRuntimeCreatorProtocol,
    UiPathRuntimeScannerProtocol,
    UiPathDisposableProtocol,
    Protocol,
):
    """Protocol for discovering and creating UiPath runtime instances."""
