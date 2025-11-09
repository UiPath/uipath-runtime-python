"""Protocols for creating UiPath runtime instances."""

from typing import Protocol

from uipath.runtime.base import UiPathRuntimeProtocol


class UiPathRuntimeScanner(Protocol):
    """Protocol for discovering all UiPath runtime instances."""

    def discover_runtimes(self) -> list[UiPathRuntimeProtocol]:
        """Discover all runtime classes."""
        ...


class UiPathRuntimeCreator(Protocol):
    """Protocol for creating a UiPath runtime given an entrypoint."""

    def new_runtime(self, entrypoint: str) -> UiPathRuntimeProtocol:
        """Create a new runtime instance."""
        ...
