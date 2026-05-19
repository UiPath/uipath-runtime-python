"""Runtime wrapper registry for cross-cutting concerns like governance.

This module provides the extension point for wrapping runtimes with
additional functionality (compliance, observability, security, etc.)
without modifying core runtime code.

Usage:
    # Register a wrapper via entry points in pyproject.toml:
    # [project.entry-points."uipath.runtime.wrappers"]
    # compliance = "my_package:register_compliance_wrapper"

    # Or register programmatically:
    from uipath.runtime import runtime_wrapper_registry

    async def my_wrapper(runtime, context, runtime_id):
        return MyWrappedRuntime(runtime, context)

    runtime_wrapper_registry.register("my_wrapper", my_wrapper, priority=100)
"""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING, Callable, Protocol

if TYPE_CHECKING:
    from uipath.runtime.base import UiPathRuntimeProtocol
    from uipath.runtime.context import UiPathRuntimeContext

logger = logging.getLogger(__name__)

# Entry point group name for auto-discovery
WRAPPER_ENTRY_POINT_GROUP = "uipath.runtime.wrappers"


class UiPathRuntimeWrapperProtocol(Protocol):
    """Protocol for runtime wrapper functions.

    A wrapper function receives a runtime and context, and returns a wrapped
    runtime that adds cross-cutting functionality (governance, tracing, etc.).

    The wrapper should preserve the UiPathRuntimeProtocol interface.
    Wrappers are async to support initialization that requires I/O
    (e.g., fetching remote policies).
    """

    async def __call__(
        self,
        runtime: UiPathRuntimeProtocol,
        context: UiPathRuntimeContext | None,
        runtime_id: str,
    ) -> UiPathRuntimeProtocol:
        """Wrap a runtime with additional functionality.

        Args:
            runtime: The runtime to wrap
            context: Runtime context (may be None)
            runtime_id: Unique identifier for this runtime instance

        Returns:
            Wrapped runtime implementing UiPathRuntimeProtocol
        """
        ...


WrapperCallable = Callable[
    ["UiPathRuntimeProtocol", "UiPathRuntimeContext | None", str],
    "UiPathRuntimeProtocol",
]


class UiPathRuntimeWrapperRegistry:
    """Registry for runtime wrappers.

    Wrappers are applied in priority order (lowest first) when creating
    runtimes. This enables layered functionality like:
    - Governance/compliance (priority 100)
    - Observability/tracing (priority 200)
    - Security/audit (priority 300)

    Wrappers can be registered via:
    1. Entry points (auto-discovered at first use)
    2. Programmatic registration via register()

    This class uses instance-level state to avoid shared mutable class
    variables. A default global instance is provided for convenience.
    """

    def __init__(self) -> None:
        """Initialize a new wrapper registry instance."""
        self._wrappers: dict[str, tuple[WrapperCallable, int]] = {}
        self._entry_points_loaded: bool = False

    def register(
        self,
        name: str,
        wrapper: WrapperCallable,
        priority: int = 100,
    ) -> None:
        """Register a runtime wrapper.

        Args:
            name: Unique wrapper identifier
            wrapper: Callable that wraps a runtime (sync or async)
            priority: Execution order (lower = applied first, default 100)
        """
        self._wrappers[name] = (wrapper, priority)
        logger.debug(f"Registered runtime wrapper: {name} (priority={priority})")

    def unregister(self, name: str) -> bool:
        """Unregister a wrapper by name.

        Args:
            name: Wrapper identifier to remove

        Returns:
            True if wrapper was removed, False if not found
        """
        if name in self._wrappers:
            del self._wrappers[name]
            logger.debug(f"Unregistered runtime wrapper: {name}")
            return True
        return False

    def load_entry_points(self) -> None:
        """Load wrappers from entry points.

        Discovers and loads all entry points in the 'uipath.runtime.wrappers'
        group. Each entry point should be a function that registers wrapper(s).
        """
        if self._entry_points_loaded:
            return

        self._entry_points_loaded = True

        if sys.version_info >= (3, 10):
            from importlib.metadata import entry_points

            eps = entry_points(group=WRAPPER_ENTRY_POINT_GROUP)
        else:
            from importlib.metadata import entry_points

            all_eps = entry_points()
            eps = all_eps.get(WRAPPER_ENTRY_POINT_GROUP, [])

        for ep in eps:
            try:
                logger.debug(f"Loading wrapper entry point: {ep.name}")
                register_func = ep.load()
                # Pass registry instance so entry points can register wrappers
                register_func(self)
                logger.debug(f"Loaded wrapper entry point: {ep.name}")
            except Exception as e:
                logger.warning(f"Failed to load wrapper entry point '{ep.name}': {e}")

    async def wrap_runtime(
        self,
        runtime: "UiPathRuntimeProtocol",
        context: "UiPathRuntimeContext | None",
        runtime_id: str,
    ) -> "UiPathRuntimeProtocol":
        """Apply all registered wrappers to a runtime.

        Wrappers are applied in priority order (lowest priority first),
        so the highest priority wrapper is the outermost layer.

        Args:
            runtime: The base runtime to wrap
            context: Runtime context
            runtime_id: Unique runtime instance identifier

        Returns:
            Wrapped runtime with all registered wrappers applied
        """
        import inspect

        # Ensure entry points are loaded
        self.load_entry_points()

        if not self._wrappers:
            return runtime

        # Sort by priority (lowest first)
        sorted_wrappers = sorted(
            self._wrappers.items(),
            key=lambda x: x[1][1],  # Sort by priority
        )

        wrapped = runtime
        for name, (wrapper, priority) in sorted_wrappers:
            try:
                result = wrapper(wrapped, context, runtime_id)
                # Support both sync and async wrappers
                if inspect.isawaitable(result):
                    wrapped = await result
                else:
                    wrapped = result
                logger.debug(f"Applied wrapper: {name} (priority={priority})")
            except Exception as e:
                logger.warning(f"Failed to apply wrapper '{name}': {e}")

        return wrapped

    def get_registered(self) -> dict[str, int]:
        """Get all registered wrappers with their priorities.

        Returns:
            Dict mapping wrapper names to priorities
        """
        self.load_entry_points()
        return {name: priority for name, (_, priority) in self._wrappers.items()}

    def clear(self) -> None:
        """Clear all registered wrappers and reset entry point loading state."""
        self._wrappers.clear()
        self._entry_points_loaded = False

    def is_registered(self, name: str) -> bool:
        """Check if a wrapper is registered.

        Args:
            name: Wrapper name to check

        Returns:
            True if registered
        """
        self.load_entry_points()
        return name in self._wrappers


# Default global registry instance for convenience
runtime_wrapper_registry = UiPathRuntimeWrapperRegistry()
