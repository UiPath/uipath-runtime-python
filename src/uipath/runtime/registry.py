"""Registry for UiPath runtime factories."""

from pathlib import Path
from typing import Callable, TypeAlias

from uipath.runtime.context import UiPathRuntimeContext
from uipath.runtime.factory import UiPathRuntimeFactoryProtocol

FactoryCallable: TypeAlias = Callable[
    [UiPathRuntimeContext | None], UiPathRuntimeFactoryProtocol
]


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
    ) -> UiPathRuntimeFactoryProtocol:
        """Get factory instance by name or auto-detect from config files.

        Args:
            name: Optional factory name
            search_path: Path to search for config files
            context: UiPathRuntimeContext to pass to factory

        Returns:
            Factory instance
        """
        if name:
            if name not in cls._factories:
                raise ValueError(f"Factory '{name}' not registered")
            factory_callable, _ = cls._factories[name]
            return factory_callable(context)

        # Auto-detect based on config files in reverse registration order
        search_dir = Path(search_path)
        for factory_name in reversed(cls._registration_order):
            factory_callable, config_file = cls._factories[factory_name]
            if (search_dir / config_file).exists():
                return factory_callable(context)

        # Fallback to default
        if cls._default_name is None:
            raise ValueError("No default factory registered and no config file found")
        factory_callable, _ = cls._factories[cls._default_name]
        return factory_callable(context)

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
