"""Base runtime class and async context manager implementation."""

import logging
from typing import (
    Any,
    AsyncGenerator,
    Literal,
    Protocol,
)

from pydantic import BaseModel, Field
from uipath.core import UiPathTraceManager

from uipath.runtime.events import (
    UiPathRuntimeEvent,
)
from uipath.runtime.logging import UiPathRuntimeExecutionLogHandler
from uipath.runtime.logging._interceptor import UiPathRuntimeLogsInterceptor
from uipath.runtime.result import UiPathRuntimeResult
from uipath.runtime.schema import (
    UiPathRuntimeSchema,
)

logger = logging.getLogger(__name__)


class UiPathStreamNotSupportedError(NotImplementedError):
    """Raised when a runtime does not support streaming."""

    pass


class UiPathExecuteOptions(BaseModel):
    """Execution-time options controlling runtime behavior."""

    resume: bool = Field(
        default=False,
        description="Indicates whether to resume a suspended execution.",
    )
    breakpoints: list[str] | Literal["*"] | None = Field(
        default=None,
        description="List of nodes or '*' to break on all steps.",
    )

    model_config = {"arbitrary_types_allowed": True, "extra": "allow"}


class UiPathStreamOptions(UiPathExecuteOptions):
    """Streaming-specific execution options."""

    pass


class UiPathExecutableProtocol(Protocol):
    """UiPath execution interface."""

    async def execute(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathExecuteOptions | None = None,
    ) -> UiPathRuntimeResult:
        """Execute the runtime with the given input and options."""
        ...


class UiPathStreamableProtocol(Protocol):
    """UiPath streaming interface."""

    async def stream(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathStreamOptions | None = None,
    ) -> AsyncGenerator[UiPathRuntimeEvent, None]:
        """Stream execution events in real-time.

        Yields framework-agnostic BaseEvent instances during execution,
        with the final event being UiPathRuntimeResult.

        Yields:
            UiPathRuntimeEvent subclasses: Framework-agnostic events (UiPathRuntimeMessageEvent,
                                  UiPathRuntimeStateEvent, etc.)
            Final yield: UiPathRuntimeResult (or its subclass UiPathBreakpointResult)

        Raises:
            UiPathRuntimeError: If execution fails

        Example:
            async for event in runtime.stream():
                if isinstance(event, UiPathRuntimeResult):
                    # Last event - execution complete
                    print(f"Status: {event.status}")
                    break
                elif isinstance(event, UiPathRuntimeMessageEvent):
                    # Handle message event
                    print(f"Message: {event.payload}")
                elif isinstance(event, UiPathRuntimeStateEvent):
                    # Handle state update
                    print(f"State updated by: {event.node_name}")
        """
        raise UiPathStreamNotSupportedError(
            f"{self.__class__.__name__} does not implement streaming. "
            "Use execute() instead."
        )
        yield


class UiPathSchemaProtocol(Protocol):
    """Contains runtime input and output schema."""

    async def get_schema(self) -> UiPathRuntimeSchema:
        """Get schema for a runtime.

        Returns: The runtime's schema (entrypoint type, input/output json schema).
        """
        ...


class UiPathDisposableProtocol(Protocol):
    """UiPath disposable interface."""

    async def dispose(self) -> None:
        """Close and clean up resources."""
        ...


# Note: explicitly marking it as a protocol for mypy.
# https://mypy.readthedocs.io/en/stable/protocols.html#defining-subprotocols-and-subclassing-protocols
# Note that inheriting from an existing protocol does not automatically turn the subclass into a protocol
# – it just creates a regular (non-protocol) class or ABC that implements the given protocol (or protocols).
# The Protocol base class must always be explicitly present if you are defining a protocol.
class UiPathRuntimeProtocol(
    UiPathExecutableProtocol,
    UiPathStreamableProtocol,
    UiPathSchemaProtocol,
    UiPathDisposableProtocol,
    Protocol,
):
    """UiPath Runtime Protocol."""


class UiPathExecutionRuntime:
    """Handles runtime execution with tracing/telemetry."""

    def __init__(
        self,
        delegate: UiPathRuntimeProtocol,
        trace_manager: UiPathTraceManager,
        root_span: str = "root",
        span_attributes: dict[str, str] | None = None,
        log_handler: UiPathRuntimeExecutionLogHandler | None = None,
        execution_id: str | None = None,
    ):
        """Initialize the executor."""
        self.delegate = delegate
        self.trace_manager = trace_manager
        self.root_span = root_span
        self.span_attributes = span_attributes
        self.execution_id = execution_id
        self.log_handler = log_handler
        if execution_id is not None and log_handler is None:
            self.log_handler = UiPathRuntimeExecutionLogHandler(execution_id)

    async def execute(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathExecuteOptions | None = None,
    ) -> UiPathRuntimeResult:
        """Execute runtime with context."""
        if self.log_handler:
            log_interceptor = UiPathRuntimeLogsInterceptor(
                execution_id=self.execution_id, log_handler=self.log_handler
            )
            log_interceptor.setup()

        try:
            if self.execution_id:
                with self.trace_manager.start_execution_span(
                    self.root_span,
                    execution_id=self.execution_id,
                    attributes=self.span_attributes,
                ):
                    return await self.delegate.execute(input, options=options)
            else:
                return await self.delegate.execute(input, options=options)
        finally:
            self.trace_manager.flush_spans()
            if self.log_handler:
                log_interceptor.teardown()

    async def stream(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathStreamOptions | None = None,
    ) -> AsyncGenerator[UiPathRuntimeEvent, None]:
        """Stream runtime execution with context.

        Args:
            runtime: The runtime instance
            context: The runtime context

        Yields:
            UiPathRuntimeEvent instances during execution and final UiPathRuntimeResult

        Raises:
            UiPathStreamNotSupportedError: If the runtime doesn't support streaming
        """
        if self.log_handler:
            log_interceptor = UiPathRuntimeLogsInterceptor(
                execution_id=self.execution_id, log_handler=self.log_handler
            )
            log_interceptor.setup()
        try:
            if self.execution_id:
                with self.trace_manager.start_execution_span(
                    self.root_span,
                    execution_id=self.execution_id,
                    attributes=self.span_attributes,
                ):
                    async for event in self.delegate.stream(input, options=options):
                        yield event
            else:
                async for event in self.delegate.stream(input, options=options):
                    yield event
        finally:
            self.trace_manager.flush_spans()
            if self.log_handler:
                log_interceptor.teardown()

    async def get_schema(self) -> UiPathRuntimeSchema:
        """Passthrough schema for the delegate."""
        return await self.delegate.get_schema()
