"""Base runtime class and async context manager implementation."""

import logging
from abc import ABC, abstractmethod
from typing import (
    Any,
    AsyncGenerator,
    Generic,
    Literal,
    Optional,
    TypeVar,
)

from pydantic import BaseModel, Field
from typing_extensions import override
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
    breakpoints: Optional[list[str] | Literal["*"]] = Field(
        default=None,
        description="List of nodes or '*' to break on all steps.",
    )

    model_config = {"arbitrary_types_allowed": True, "extra": "allow"}


class UiPathStreamOptions(UiPathExecuteOptions):
    """Streaming-specific execution options."""

    pass


class UiPathBaseRuntime(ABC):
    """Base runtime class implementing the async context manager protocol.

    This allows using the class with 'async with' statements.
    """

    async def get_schema(self) -> UiPathRuntimeSchema:
        """Get schema for this runtime.

        Returns: The runtime's schema (entrypoint type, input/output json schema).
        """
        raise NotImplementedError()

    @abstractmethod
    async def execute(
        self,
        input: Optional[dict[str, Any]] = None,
        options: Optional[UiPathExecuteOptions] = None,
    ) -> UiPathRuntimeResult:
        """Produce the agent output."""
        raise NotImplementedError()

    async def stream(
        self,
        input: Optional[dict[str, Any]] = None,
        options: Optional[UiPathStreamOptions] = None,
    ) -> AsyncGenerator[UiPathRuntimeEvent, None]:
        """Stream execution events in real-time.

        This is an optional method that runtimes can implement to support streaming.
        If not implemented, only the execute() method will be available.

        Yields framework-agnostic BaseEvent instances during execution,
        with the final event being UiPathRuntimeResult.

        Yields:
            UiPathRuntimeEvent subclasses: Framework-agnostic events (UiPathRuntimeMessageEvent,
                                  UiPathRuntimeStateEvent, etc.)
            Final yield: UiPathRuntimeResult (or its subclass UiPathBreakpointResult)

        Raises:
            UiPathStreamNotSupportedError: If the runtime doesn't support streaming
            RuntimeError: If execution fails

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
        # This yield is unreachable but makes this a proper generator function
        # Without it, the function wouldn't match the AsyncGenerator return type
        yield

    @abstractmethod
    async def cleanup(self):
        """Cleaup runtime resources."""
        pass


T = TypeVar("T", bound=UiPathBaseRuntime)


class UiPathExecutionRuntime(UiPathBaseRuntime, Generic[T]):
    """Handles runtime execution with tracing/telemetry."""

    def __init__(
        self,
        delegate: T,
        trace_manager: UiPathTraceManager,
        root_span: str = "root",
        log_handler: Optional[UiPathRuntimeExecutionLogHandler] = None,
        execution_id: Optional[str] = None,
    ):
        """Initialize the executor."""
        self.delegate = delegate
        self.trace_manager = trace_manager
        self.root_span = root_span
        self.execution_id = execution_id
        self.log_handler = log_handler
        if execution_id is not None and log_handler is None:
            self.log_handler = UiPathRuntimeExecutionLogHandler(execution_id)

    async def execute(
        self,
        input: Optional[dict[str, Any]] = None,
        options: Optional[UiPathExecuteOptions] = None,
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
                    self.root_span, execution_id=self.execution_id
                ):
                    return await self.delegate.execute(input, options=options)
            else:
                return await self.delegate.execute(input, options=options)
        finally:
            self.trace_manager.flush_spans()
            if self.log_handler:
                log_interceptor.teardown()

    @override
    async def stream(
        self,
        input: Optional[dict[str, Any]] = None,
        options: Optional[UiPathStreamOptions] = None,
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
                    self.root_span, execution_id=self.execution_id
                ):
                    async for event in self.delegate.stream(input, options=options):
                        yield event
        finally:
            self.trace_manager.flush_spans()
            if self.log_handler:
                log_interceptor.teardown()

    def cleanup(self) -> None:
        """Close runtime resources."""
        pass
