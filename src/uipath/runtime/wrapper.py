"""Runtime wrapper registry for governance integration.

This module provides the extension point for the uipath-governance package
to wrap runtimes with policy evaluation. Only explicitly allowed wrappers
can be registered to prevent unauthorized interception of runtime executions.

The wrapper system uses entry points to avoid circular dependencies:
- uipath-runtime defines the extension point (this module)
- uipath-governance registers itself via entry points
- uipath-runtime never imports uipath-governance directly
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

# Allowed wrapper names - only these can be registered.
# This prevents malicious packages from injecting wrappers.
ALLOWED_WRAPPERS: frozenset[str] = frozenset(
    {
        "governance",  # uipath-governance package
    }
)

# Allowed packages that can register wrappers via entry points.
# Entry points from other packages are ignored.
ALLOWED_WRAPPER_PACKAGES: frozenset[str] = frozenset(
    {
        "uipath-governance",
    }
)


class UiPathRuntimeWrapperProtocol(Protocol):
    """Protocol for runtime wrapper functions."""

    async def __call__(
        self,
        runtime: UiPathRuntimeProtocol,
        context: UiPathRuntimeContext | None,
        runtime_id: str,
    ) -> UiPathRuntimeProtocol:
        """Wrap a runtime with additional functionality."""
        ...


WrapperCallable = Callable[
    ["UiPathRuntimeProtocol", "UiPathRuntimeContext | None", str],
    "UiPathRuntimeProtocol",
]


class UiPathRuntimeWrapperRegistry:
    """Registry for runtime wrappers.

    Security:
        Only wrappers with names in ALLOWED_WRAPPERS can be registered.
        Only entry points from packages in ALLOWED_WRAPPER_PACKAGES are loaded.
        This prevents malicious packages from intercepting runtime executions.

    Wrappers are applied in priority order (lowest first) when creating
    runtimes. Currently only governance is allowed at priority 100.
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
            name: Unique wrapper identifier (must be in ALLOWED_WRAPPERS)
            wrapper: Callable that wraps a runtime (sync or async)
            priority: Execution order (lower = applied first, default 100)

        Raises:
            ValueError: If wrapper name is not in ALLOWED_WRAPPERS
        """
        if name not in ALLOWED_WRAPPERS:
            logger.warning(
                f"Rejected wrapper registration: '{name}' is not in allowed list. "
                f"Allowed wrappers: {sorted(ALLOWED_WRAPPERS)}"
            )
            raise ValueError(
                f"Wrapper '{name}' is not allowed. "
                f"Only these wrappers can be registered: {sorted(ALLOWED_WRAPPERS)}"
            )

        if name in self._wrappers:
            logger.debug(f"Wrapper '{name}' already registered, skipping")
            return

        self._wrappers[name] = (wrapper, priority)
        logger.debug(f"Registered runtime wrapper: {name} (priority={priority})")

    def unregister(self, name: str) -> bool:
        """Unregister a wrapper by name."""
        if name in self._wrappers:
            del self._wrappers[name]
            logger.debug(f"Unregistered runtime wrapper: {name}")
            return True
        return False

    def _get_package_name_from_entry_point(self, ep) -> str | None:
        """Extract package name from entry point."""
        try:
            if hasattr(ep, "dist") and ep.dist is not None:
                return ep.dist.name
        except Exception:
            pass

        # Fallback: derive from module name
        try:
            module_name = ep.value.split(":")[0]
            top_level = module_name.split(".")[0]
            return top_level.replace("_", "-")
        except Exception:
            return None

    def load_entry_points(self) -> None:
        """Load wrappers from entry points.

        Only entry points from packages in ALLOWED_WRAPPER_PACKAGES are loaded.
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
            package_name = self._get_package_name_from_entry_point(ep)

            if package_name is None:
                logger.warning(
                    f"Could not determine package for entry point '{ep.name}', skipping"
                )
                continue

            if package_name not in ALLOWED_WRAPPER_PACKAGES:
                logger.warning(
                    f"Blocked wrapper entry point '{ep.name}' from package "
                    f"'{package_name}': not in allowed packages list"
                )
                continue

            try:
                logger.debug(f"Loading wrapper entry point: {ep.name} ({package_name})")
                register_func = ep.load()
                register_func(self)
                logger.info(f"Loaded wrapper from '{package_name}': {ep.name}")
            except ValueError as e:
                # Wrapper name not allowed
                logger.warning(f"Entry point '{ep.name}' registration failed: {e}")
            except Exception as e:
                logger.warning(f"Failed to load wrapper entry point '{ep.name}': {e}")

    async def wrap_runtime(
        self,
        runtime: "UiPathRuntimeProtocol",
        context: "UiPathRuntimeContext | None",
        runtime_id: str,
    ) -> "UiPathRuntimeProtocol":
        """Apply all registered wrappers to a runtime.

        Wrappers are applied in priority order (lowest priority first).
        """
        import inspect

        # Ensure entry points are loaded
        self.load_entry_points()

        if not self._wrappers:
            return runtime

        # Sort by priority (lowest first)
        sorted_wrappers = sorted(
            self._wrappers.items(),
            key=lambda x: x[1][1],
        )

        wrapped = runtime
        for name, (wrapper, priority) in sorted_wrappers:
            try:
                result = wrapper(wrapped, context, runtime_id)
                if inspect.isawaitable(result):
                    wrapped = await result
                else:
                    wrapped = result
                logger.debug(f"Applied wrapper: {name} (priority={priority})")
            except Exception as e:
                logger.warning(f"Failed to apply wrapper '{name}': {e}")

        return wrapped

    def get_registered(self) -> dict[str, int]:
        """Get all registered wrappers with their priorities."""
        self.load_entry_points()
        return {name: priority for name, (_, priority) in self._wrappers.items()}

    def clear(self) -> None:
        """Clear all registered wrappers and reset entry point loading."""
        self._wrappers.clear()
        self._entry_points_loaded = False

    def is_registered(self, name: str) -> bool:
        """Check if a wrapper is registered."""
        self.load_entry_points()
        return name in self._wrappers


# Default global registry instance
runtime_wrapper_registry = UiPathRuntimeWrapperRegistry()
