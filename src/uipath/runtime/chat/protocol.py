"""Abstract conversation bridge interface."""

from typing import Any, Protocol

from uipath.core.chat import (
    UiPathConversationMessageEvent,
)

from uipath.runtime.result import UiPathRuntimeResult


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

    async def emit_interrupt_event(
        self,
        interrupt_event: UiPathRuntimeResult,
    ) -> None:
        """Wrap and send an interrupt event.

        Args:
            interrupt_event: UiPathConversationInterruptEvent to wrap and send
        """
        ...

    async def emit_exchange_end_event(self) -> None:
        """Send an exchange end event."""
        ...

    async def wait_for_resume(self) -> dict[str, Any]:
        """Wait for the interrupt_end event to be received."""
        ...
