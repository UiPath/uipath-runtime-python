"""Abstract conversation bridge interface."""

from typing import Any, Protocol

from uipath.core.chat import (
    UiPathConversationMessageEvent,
)


class UiPathChatProtocol(Protocol):
    """Abstract interface for chat communication.

    Implementations: WebSocket, etc.
    """

    async def connect(self) -> None:
        """Establish connection to chat service."""
        ...

    async def disconnect(self) -> None:
        """Close connection and send exchange end event."""
        ...

    async def emit_message_event(
        self, message_event: UiPathConversationMessageEvent
    ) -> None:
        """Wrap and send a message event.

        Args:
            message_event: UiPathConversationMessageEvent to wrap and send
        """
        ...

    async def emit_exchange_end_event(self) -> None:
        """Send an exchange end event."""
        ...

    async def emit_exchange_error_event(self, error: Exception) -> None:
        """Emit an exchange error event."""
        ...

    async def wait_for_resume(self) -> dict[str, Any]:
        """Wait for a confirmToolCall event to be received."""
        ...
