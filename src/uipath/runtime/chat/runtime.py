"""Chat runtime implementation."""

import logging
from typing import Any, AsyncGenerator, cast

from uipath.core.triggers import UiPathResumeTriggerType

from uipath.runtime.base import (
    UiPathExecuteOptions,
    UiPathRuntimeProtocol,
    UiPathStreamOptions,
)
from uipath.runtime.chat.protocol import UiPathChatProtocol
from uipath.runtime.errors import UiPathBaseRuntimeError, UiPathErrorContract
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

_DEFAULT_ERROR_ID = "AGENT_RUNTIME_ERROR"
_DEFAULT_ERROR_MESSAGE = "An unexpected error occurred."


def _extract_error_from_exception(e: Exception) -> tuple[str, str]:
    """Extract error_id and user-facing message from an exception."""
    if isinstance(e, UiPathBaseRuntimeError):
        return _extract_error_from_contract(e.error_info)
    return _DEFAULT_ERROR_ID, _DEFAULT_ERROR_MESSAGE


def _extract_error_from_contract(
    error: UiPathErrorContract | None,
) -> tuple[str, str]:
    """Extract error_id and user-facing message from an error contract."""
    if not error:
        return _DEFAULT_ERROR_ID, _DEFAULT_ERROR_MESSAGE
    error_id = error.code or _DEFAULT_ERROR_ID
    title = error.title or ""
    detail = error.detail.split("\n")[0] if error.detail else ""
    if title and detail:
        error_message = f"{title}. {detail}"
    else:
        error_message = title or detail or _DEFAULT_ERROR_MESSAGE
    return error_id, error_message


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
        try:
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
                            and runtime_result.triggers
                        ):
                            api_triggers = [
                                t
                                for t in runtime_result.triggers
                                if t.trigger_type == UiPathResumeTriggerType.API
                            ]

                            if api_triggers:
                                resume_map: dict[str, Any] = {}

                                for trigger in api_triggers:
                                    await self.chat_bridge.emit_interrupt_event(trigger)

                                    resume_data = (
                                        await self.chat_bridge.wait_for_resume()
                                    )

                                    assert trigger.interrupt_id is not None, (
                                        "Trigger interrupt_id cannot be None"
                                    )
                                    resume_map[trigger.interrupt_id] = resume_data

                                current_input = resume_map
                                current_options.resume = True
                                break
                            else:
                                # No API triggers - yield result and complete
                                yield event
                                execution_completed = True
                        elif runtime_result.status == UiPathRuntimeStatus.FAULTED:
                            await self._emit_error_event(
                                *_extract_error_from_contract(runtime_result.error)
                            )
                            yield event
                            execution_completed = True
                        else:
                            yield event
                            execution_completed = True
                            await self.chat_bridge.emit_exchange_end_event()
                    else:
                        yield event

        except Exception as e:
            error_id, error_message = _extract_error_from_exception(e)
            await self._emit_error_event(error_id, error_message)
            raise

    async def _emit_error_event(self, error_id: str, message: str) -> None:
        """Emit an exchange error event to the chat bridge."""
        try:
            await self.chat_bridge.emit_exchange_error_event(
                error_id=error_id,
                message=message,
            )
        except Exception:
            logger.warning("Failed to emit exchange error event", exc_info=True)

    async def get_schema(self) -> UiPathRuntimeSchema:
        """Get schema from the delegate runtime."""
        return await self.delegate.get_schema()

    async def dispose(self) -> None:
        """Cleanup runtime resources."""
        try:
            await self.chat_bridge.disconnect()
        except Exception as e:
            logger.warning(f"Error disconnecting chat bridge: {e}")
