"""Base runtime class and async context manager implementation."""

import logging
from abc import ABC, abstractmethod
from typing import (
    Any,
    AsyncGenerator,
    Generic,
    Optional,
    TypeVar,
)

from opentelemetry import trace
from typing_extensions import override
from uipath.core import UiPathTraceManager

from uipath.runtime.context import UiPathRuntimeContext
from uipath.runtime.events import (
    UiPathRuntimeEvent,
)
from uipath.runtime.logging import UiPathRuntimeExecutionLogHandler
from uipath.runtime.logging._interceptor import UiPathRuntimeLogsInterceptor
from uipath.runtime.schema import (
    UiPathRuntimeSchema,
)

logger = logging.getLogger(__name__)


class UiPathStreamNotSupportedError(NotImplementedError):
    """Raised when a runtime does not support streaming."""

    pass


class UiPathBaseRuntime(ABC):
    """Base runtime class implementing the async context manager protocol.

    This allows using the class with 'async with' statements.
    """

    def __init__(self, context: Optional[UiPathRuntimeContext] = None):
        """Initialize the runtime with the provided context."""
        self.context = context

    async def get_schema(self) -> UiPathRuntimeSchema:
        """Get schema for this runtime.

        Returns: The runtime's schema (entrypoint type, input/output json schema).
        """
        raise NotImplementedError()

    @abstractmethod
    async def execute(self, input: dict[str, Any]) -> Any:
        """Produce the agent output."""
        raise NotImplementedError()

    async def stream(
        self,
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
        execution_id: Optional[str] = None,
    ):
        """Initialize the executor."""
        self.delegate = delegate
        self.trace_manager = trace_manager
        self.root_span = root_span
        self.execution_id = execution_id
        self.log_handler: Optional[UiPathRuntimeExecutionLogHandler]
        if execution_id is not None:
            self.log_handler = UiPathRuntimeExecutionLogHandler(execution_id)
        else:
            self.log_handler = None

    async def execute(
        self,
        input: dict[str, Any],
    ) -> Any:
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
                    return await self.delegate.execute(input)
            else:
                return await self.delegate.execute(input)
        finally:
            self.trace_manager.flush_spans()
            if self.log_handler:
                log_interceptor.teardown()

    @override
    async def stream(
        self,
        root_span: str = "root",
        execution_id: Optional[str] = None,
    ) -> AsyncGenerator[UiPathRuntimeEvent, None]:
        """Stream runtime execution with context.

        Args:
            runtime: The runtime instance
            context: The runtime context

        Yields:
            UiPathRuntimeEvent instances during execution and final UiPathRuntimeResult

        Raises:
            UiPathRuntimeStreamNotSupportedError: If the runtime doesn't support streaming
        """
        try:
            tracer = trace.get_tracer("uipath-runtime")
            span_attributes: dict[str, Any] = {}
            if execution_id:
                span_attributes["execution.id"] = "exec-a"
            with tracer.start_as_current_span(root_span, attributes=span_attributes):
                async for event in self.delegate.stream():
                    yield event
        finally:
            self.trace_manager.flush_spans()

    def cleanup(self) -> None:
        """Close runtime resources."""
        pass
