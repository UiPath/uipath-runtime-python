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
from uipath.runtime.resumable.trigger import UiPathResumeTriggerType
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

        execution_completed = False
        current_input = input
        current_options = UiPathStreamOptions(
            resume=options.resume if options else False,
            breakpoints=options.breakpoints if options else None,
        )

        while not execution_completed:
            async for event in self.delegate.stream(
                current_input, options=current_options
            ):
                if isinstance(event, UiPathRuntimeMessageEvent):
                    if event.payload:
                        await self.chat_bridge.emit_message_event(event.payload)

                if isinstance(event, UiPathRuntimeResult):
                    runtime_result = event

                    if (
                        runtime_result.status == UiPathRuntimeStatus.SUSPENDED
                        and runtime_result.trigger
                        and runtime_result.trigger.trigger_type
                        == UiPathResumeTriggerType.API
                    ):
                        await self.chat_bridge.emit_interrupt_event(runtime_result)
                        resume_data = await self.chat_bridge.wait_for_resume()

                        # Continue with resumed execution
                        current_input = resume_data
                        current_options.resume = True
                        break
                    else:
                        yield event
                        execution_completed = True
                else:
                    yield event

        await self.chat_bridge.emit_exchange_end_event()

    async def get_schema(self) -> UiPathRuntimeSchema:
        """Get schema from the delegate runtime."""
        return await self.delegate.get_schema()

    async def dispose(self) -> None:
        """Cleanup runtime resources."""
        try:
            await self.chat_bridge.disconnect()
        except Exception as e:
            logger.warning(f"Error disconnecting chat bridge: {e}")
