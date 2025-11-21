"""Module defining the protocol for resume trigger storage."""

from typing import Any, Protocol

from uipath.runtime.resumable.trigger import UiPathResumeTrigger


class UiPathResumableStorageProtocol(Protocol):
    """Protocol for storing and retrieving resume triggers."""

    async def save_trigger(self, trigger: UiPathResumeTrigger) -> None:
        """Save a resume trigger to storage.

        Args:
            trigger: The resume trigger to persist

        Raises:
            Exception: If storage operation fails
        """
        ...

    async def get_latest_trigger(self) -> UiPathResumeTrigger | None:
        """Retrieve the most recent resume trigger from storage.

        Returns:
            The latest resume trigger, or None if no triggers exist

        Raises:
            Exception: If retrieval operation fails
        """
        ...


class UiPathResumeTriggerCreatorProtocol(Protocol):
    """Protocol for creating resume triggers from suspend values."""

    async def create_trigger(self, suspend_value: Any) -> UiPathResumeTrigger:
        """Create a resume trigger from a suspend value.

        Args:
            suspend_value: The value that caused the suspension.
                Can be UiPath models (CreateAction, InvokeProcess, etc.),
                strings, or any other value that needs HITL processing.

        Returns:
            UiPathResumeTrigger ready to be persisted

        Raises:
            UiPathRuntimeError: If trigger creation fails
        """
        ...


class UiPathResumeTriggerReaderProtocol(Protocol):
    """Protocol for reading resume triggers and converting them to runtime input."""

    async def read_trigger(self, trigger: UiPathResumeTrigger) -> Any | None:
        """Read a resume trigger and convert it to runtime-compatible input.

        This method retrieves data from UiPath services (Actions, Jobs, API)
        based on the trigger type and returns it in a format that the
        runtime can use to resume execution.

        Args:
            trigger: The resume trigger to read

        Returns:
            The data retrieved from UiPath services, ready to be used
            as resume input. Format depends on trigger type:
            - ACTION: Action data (possibly with escalation processing)
            - JOB: Job output data
            - API: API payload
            Returns None if no data is available.

        Raises:
            UiPathRuntimeError: If reading fails or job failed
        """
        ...


class UiPathResumeTriggerProtocol(
    UiPathResumeTriggerCreatorProtocol, UiPathResumeTriggerReaderProtocol, Protocol
):
    """Protocol combining both creation and reading of resume triggers."""
