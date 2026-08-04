"""Storage-backed workspace attachment registry."""

from typing import Any

from uipath.runtime.storage import UiPathRuntimeStorageProtocol


class WorkspaceRegistryStore:
    """Stores workspace file attachment metadata in runtime storage."""

    DEFAULT_NAMESPACE = "workspace"
    DEFAULT_KEY = "attachments"

    def __init__(
        self,
        storage: UiPathRuntimeStorageProtocol,
        runtime_id: str,
        *,
        namespace: str = DEFAULT_NAMESPACE,
        key: str = DEFAULT_KEY,
    ):
        """Initialize the registry store."""
        self.storage = storage
        self.runtime_id = runtime_id
        self.namespace = namespace
        self.key = key

    async def try_load(self) -> dict[str, dict[str, Any]] | None:
        """Load the registry, or return ``None`` when it has not been saved."""
        value = await self.storage.get_value(self.runtime_id, self.namespace, self.key)
        if value is None:
            return None
        if not isinstance(value, dict):
            raise TypeError("Workspace registry payload must be a dictionary.")

        registry: dict[str, dict[str, Any]] = {}
        for path, entry in value.items():
            if isinstance(path, str) and isinstance(entry, dict):
                registry[path] = entry
        return registry

    async def load(self) -> dict[str, dict[str, Any]]:
        """Load registry entries, defaulting to an empty registry."""
        registry = await self.try_load()
        return registry if registry is not None else {}

    async def save(self, registry: dict[str, dict[str, Any]]) -> None:
        """Persist registry entries."""
        await self.storage.set_value(
            self.runtime_id,
            self.namespace,
            self.key,
            registry,
        )
