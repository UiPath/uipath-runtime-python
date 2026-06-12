"""Registry for UiPath runtime factories."""

from pathlib import Path
from typing import Callable, TypeAlias

from uipath.runtime.base import UiPathRuntimeProtocol
from uipath.runtime.context import UiPathRuntimeContext
from uipath.runtime.factory import (
    UiPathRuntimeFactoryProtocol,
    UiPathRuntimeFactorySettings,
)
from uipath.runtime.storage import UiPathRuntimeStorageProtocol
from uipath.runtime.wrapper import apply_governance_wrapper

FactoryCallable: TypeAlias = Callable[
    [UiPathRuntimeContext | None], UiPathRuntimeFactoryProtocol
]


class UiPathWrappedRuntimeFactory(UiPathRuntimeFactoryProtocol):
    """Factory that delegates creation and applies governance to every runtime.

    Implements ``UiPathRuntimeFactoryProtocol`` so callers using the
    protocol surface are unaffected. Non-protocol attribute access falls
    through to the underlying factory via ``__getattr__``; the underlying
    factory is also reachable directly via the :attr:`inner` property —
    useful for callers that need the concrete registered type (e.g.
    ``isinstance`` checks) and for tests.
    """

    def __init__(
        self,
        delegate: UiPathRuntimeFactoryProtocol,
        context: UiPathRuntimeContext | None = None,
    ) -> None:
        """Initialize with the underlying factory and the runtime context."""
        self._delegate = delegate
        self._context = context

    @property
    def inner(self) -> UiPathRuntimeFactoryProtocol:
        """Return the underlying registered factory.

        Use this when a caller needs the concrete factory type
        (``isinstance`` checks, access to non-protocol public API).
        Prefer passing ``apply_wrappers=False`` to
        :meth:`UiPathRuntimeFactoryRegistry.get` when you want the
        registry to return the concrete factory directly.
        """
        return self._delegate

    def discover_entrypoints(self) -> list[str]:
        """Delegate to the underlying factory."""
        return self._delegate.discover_entrypoints()

    async def new_runtime(
        self, entrypoint: str, runtime_id: str, **kwargs
    ) -> UiPathRuntimeProtocol:
        """Create a runtime via the delegate and apply governance."""
        runtime = await self._delegate.new_runtime(entrypoint, runtime_id, **kwargs)
        return await apply_governance_wrapper(runtime, self._context, runtime_id)

    async def get_storage(self) -> UiPathRuntimeStorageProtocol | None:
        """Delegate to the underlying factory."""
        return await self._delegate.get_storage()

    async def get_settings(self) -> UiPathRuntimeFactorySettings | None:
        """Delegate to the underlying factory."""
        return await self._delegate.get_settings()

    async def dispose(self) -> None:
        """Delegate to the underlying factory."""
        return await self._delegate.dispose()

    def __getattr__(self, name: str):
        """Forward attribute lookups to the delegate.

        Only invoked when normal attribute resolution fails on this
        wrapper. Lets callers reach any non-protocol public API on the
        registered concrete factory without knowing they hold a wrapper.
        ``isinstance`` checks still see this class, not the delegate —
        use :attr:`inner` or ``apply_wrappers=False`` for those.
        """
        # Guard against recursion during construction (before _delegate is set).
        if name == "_delegate":
            raise AttributeError(name)
        return getattr(self._delegate, name)


class UiPathRuntimeFactoryRegistry:
    """Registry for UiPath runtime factories."""

    _factories: dict[str, tuple[FactoryCallable, str]] = {}
    _registration_order: list[str] = []
    _default_name: str | None = None

    @classmethod
    def register(
        cls, name: str, factory_callable: FactoryCallable, config_file: str
    ) -> None:
        """Register factory callable with its config file indicator.

        Args:
            name: Factory identifier
            factory_callable: Callable that accepts context and returns a factory instance
            config_file: Config file name that indicates this factory should be used
        """
        if name in cls._factories:
            cls._registration_order.remove(name)

        cls._factories[name] = (factory_callable, config_file)
        cls._registration_order.append(name)

    @classmethod
    def get(
        cls,
        name: str | None = None,
        search_path: str = ".",
        context: UiPathRuntimeContext | None = None,
        apply_wrappers: bool = True,
    ) -> UiPathRuntimeFactoryProtocol:
        """Get factory instance by name or auto-detect from config files.

        Args:
            name: Optional factory name
            search_path: Path to search for config files
            context: UiPathRuntimeContext to pass to factory
            apply_wrappers: When True (default), the registered factory
                is wrapped in :class:`UiPathWrappedRuntimeFactory` so
                every runtime it produces passes through
                :func:`apply_governance_wrapper`. Set False to obtain the
                concrete registered factory unchanged — required for
                callers that ``isinstance``-check the result or rely on
                non-protocol public API. The wrapper exposes the
                underlying factory via its :attr:`inner` property and
                forwards unknown attribute access to it, but its type is
                still :class:`UiPathWrappedRuntimeFactory`.

        Returns:
            A :class:`UiPathRuntimeFactoryProtocol`; concretely a
            :class:`UiPathWrappedRuntimeFactory` when
            ``apply_wrappers=True``, otherwise the registered factory.
        """
        factory: UiPathRuntimeFactoryProtocol | None = None

        if name:
            if name not in cls._factories:
                raise ValueError(f"Factory '{name}' not registered")
            factory_callable, _ = cls._factories[name]
            factory = factory_callable(context)
        else:
            # Auto-detect based on config files in reverse registration order
            search_dir = Path(search_path)
            for factory_name in reversed(cls._registration_order):
                factory_callable, config_file = cls._factories[factory_name]
                if (search_dir / config_file).exists():
                    factory = factory_callable(context)
                    break

            # Fallback to default
            if factory is None:
                if cls._default_name is None:
                    raise ValueError(
                        "No default factory registered and no config file found"
                    )
                factory_callable, _ = cls._factories[cls._default_name]
                factory = factory_callable(context)

        # Wrap factory to auto-apply runtime wrappers
        if apply_wrappers:
            factory = UiPathWrappedRuntimeFactory(factory, context)

        return factory

    @classmethod
    def set_default(cls, name: str) -> None:
        """Set a factory as default."""
        if name not in cls._factories:
            raise ValueError(f"Factory '{name}' not registered")
        cls._default_name = name

    @classmethod
    def get_all(cls) -> dict[str, str]:
        """Get all registered factories.

        Returns:
            Dict mapping factory names to their config files
        """
        return {name: config_file for name, (_, config_file) in cls._factories.items()}
