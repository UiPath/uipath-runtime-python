"""Abstract conversation bridge interface."""

from typing import Protocol

from uipath.core.chat import UiPathConversationMessageEvent


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
