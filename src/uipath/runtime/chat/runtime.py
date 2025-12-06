"""Chat runtime implementation."""

import logging
from typing import Any, AsyncGenerator, cast

from uipath.runtime.base import (
    UiPathExecuteOptions,
    UiPathRuntimeProtocol,
    UiPathStreamOptions,
)
from uipath.runtime.chat.protocol import UiPathChatProtocol
from uipath.runtime.events import (
    UiPathRuntimeEvent,
    UiPathRuntimeMessageEvent,
)
from uipath.runtime.result import (
    UiPathRuntimeResult,
    UiPathRuntimeStatus,
)
from uipath.runtime.schema import UiPathRuntimeSchema

logger = logging.getLogger(__name__)


class UiPathChatRuntime:
    """Specialized runtime for chat mode that streams message events to a chat bridge."""

    def __init__(
        self,
        delegate: UiPathRuntimeProtocol,
        chat_bridge: UiPathChatProtocol,
    ):
        """Initialize the UiPathChatRuntime.

        Args:
            delegate: The underlying runtime to wrap
            chat_bridge: Bridge for chat event communication
        """
        super().__init__()
        self.delegate = delegate
        self.chat_bridge = chat_bridge

    async def execute(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathExecuteOptions | None = None,
    ) -> UiPathRuntimeResult:
        """Execute the workflow with chat support."""
        result: UiPathRuntimeResult | None = None
        async for event in self.stream(input, cast(UiPathStreamOptions, options)):
            if isinstance(event, UiPathRuntimeResult):
                result = event

        return (
            result
            if result
            else UiPathRuntimeResult(status=UiPathRuntimeStatus.SUCCESSFUL)
        )

    async def stream(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathStreamOptions | None = None,
    ) -> AsyncGenerator[UiPathRuntimeEvent, None]:
        """Stream execution events with chat support."""
        await self.chat_bridge.connect()

        async for event in self.delegate.stream(input, options=options):
            if isinstance(event, UiPathRuntimeMessageEvent):
                if event.payload:
                    await self.chat_bridge.emit_message_event(event.payload)

            yield event

    async def get_schema(self) -> UiPathRuntimeSchema:
        """Get schema from the delegate runtime."""
        return await self.delegate.get_schema()

    async def dispose(self) -> None:
        """Cleanup runtime resources."""
        try:
            await self.chat_bridge.disconnect()
        except Exception as e:
            logger.warning(f"Error disconnecting chat bridge: {e}")
