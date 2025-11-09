"""Factory for creating UiPath runtime instances."""

from abc import ABC, abstractmethod
from typing import (
    Generic,
    TypeVar,
)

from uipath.runtime.base import UiPathBaseRuntime

T = TypeVar("T", bound=UiPathBaseRuntime)


class UiPathRuntimeFactory(Generic[T], ABC):
    """Generic factory for UiPath runtime classes."""

    @abstractmethod
    def discover_runtimes(self) -> list[T]:
        """Discover all runtime classes."""
        raise NotImplementedError()

    @abstractmethod
    def new_runtime(self, entrypoint: str) -> T:
        """Create a new runtime instance."""
        raise NotImplementedError()
