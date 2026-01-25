"""Runtime storage protocol definition."""

from typing import (
    Any,
    Protocol,
)


class UiPathRuntimeStorageProtocol(Protocol):
    """Protocol for runtime storage operations."""

    async def set_value(
        self, runtime_id: str, namespace: str, key: str, value: Any
    ) -> None:
        """Store values for a specific runtime.

        Args:
            runtime_id: The runtime ID
            namespace: The namespace of the persisted value
            key: The key associated with the persisted value
            value: The value to persist

        Raises:
            Exception: If storage operation fails
        """
        ...

    async def get_value(self, runtime_id: str, namespace: str, key: str) -> Any:
        """Retrieve values for a specific runtime from storage.

        Args:
            runtime_id: The runtime ID
            namespace: The namespace of the persisted value
            key: The key associated with the persisted value

        Returns:
            The value matching the method's parameters, or None if it does not exist

        Raises:
            Exception: If retrieval operation fails
        """
        ...
