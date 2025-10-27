"""Factory for creating UiPath runtime instances."""

from typing import (
    Any,
    AsyncGenerator,
    Callable,
    Generic,
    List,
    Optional,
    Type,
    TypeVar,
    Union,
)

from opentelemetry import trace
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor  # type: ignore
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import (
    SpanExporter,
)
from opentelemetry.trace import Tracer

from uipath.runtime.base import UiPathBaseRuntime
from uipath.runtime.context import UiPathRuntimeContext
from uipath.runtime.events import UiPathRuntimeEvent
from uipath.runtime.result import UiPathRuntimeResult
from uipath.runtime.tracing import (
    UiPathExecutionBatchTraceProcessor,
    UiPathExecutionSimpleTraceProcessor,
    UiPathRuntimeTracingManager,
)

T = TypeVar("T", bound="UiPathBaseRuntime")
C = TypeVar("C", bound="UiPathRuntimeContext")


class UiPathRuntimeFactory(Generic[T, C]):
    """Generic factory for UiPath runtime classes."""

    def __init__(
        self,
        runtime_class: Type[T],
        context_class: Type[C],
        runtime_generator: Optional[Callable[[C], T]] = None,
        context_generator: Optional[Callable[..., C]] = None,
    ):
        """Initialize the UiPathRuntimeFactory."""
        if not issubclass(runtime_class, UiPathBaseRuntime):
            raise TypeError(
                f"runtime_class {runtime_class.__name__} must inherit from UiPathBaseRuntime"
            )

        if not issubclass(context_class, UiPathRuntimeContext):
            raise TypeError(
                f"context_class {context_class.__name__} must inherit from UiPathRuntimeContext"
            )

        self.runtime_class = runtime_class
        self.context_class = context_class
        self.runtime_generator = runtime_generator
        self.context_generator = context_generator
        self.tracer_provider: TracerProvider = TracerProvider()
        self.tracer_span_processors: List[SpanProcessor] = []
        self.logs_exporter: Optional[Any] = None
        trace.set_tracer_provider(self.tracer_provider)

    def add_span_exporter(
        self,
        span_exporter: SpanExporter,
        batch: bool = True,
    ) -> "UiPathRuntimeFactory[T, C]":
        """Add a span processor to the tracer provider."""
        span_processor: SpanProcessor
        if batch:
            span_processor = UiPathExecutionBatchTraceProcessor(span_exporter)
        else:
            span_processor = UiPathExecutionSimpleTraceProcessor(span_exporter)
        self.tracer_span_processors.append(span_processor)
        self.tracer_provider.add_span_processor(span_processor)
        return self

    def add_instrumentor(
        self,
        instrumentor_class: Type[BaseInstrumentor],
        get_current_span_func: Callable[[], Any],
    ) -> "UiPathRuntimeFactory[T, C]":
        """Add and instrument immediately."""
        instrumentor_class().instrument(tracer_provider=self.tracer_provider)
        UiPathRuntimeTracingManager.register_current_span_provider(
            get_current_span_func
        )
        return self

    def new_context(self, **kwargs) -> C:
        """Create a new context instance."""
        if self.context_generator:
            return self.context_generator(**kwargs)
        return self.context_class(**kwargs)

    def new_runtime(self, **kwargs) -> T:
        """Create a new runtime instance."""
        context = self.new_context(**kwargs)
        if self.runtime_generator:
            return self.runtime_generator(context)
        return self.runtime_class.from_context(context)

    def from_context(self, context: C) -> T:
        """Create runtime instance from context."""
        if self.runtime_generator:
            return self.runtime_generator(context)
        return self.runtime_class.from_context(context)

    async def execute(self, context: C) -> Optional[UiPathRuntimeResult]:
        """Execute runtime with context."""
        async with self.from_context(context) as runtime:
            try:
                return await runtime.execute()
            finally:
                for span_processor in self.tracer_span_processors:
                    span_processor.force_flush()

    async def stream(
        self, context: C
    ) -> AsyncGenerator[Union[UiPathRuntimeEvent, UiPathRuntimeResult], None]:
        """Stream runtime execution with context.

        Args:
            context: The runtime context

        Yields:
            UiPathRuntimeEvent instances during execution and final UiPathRuntimeResult

        Raises:
            UiPathRuntimeStreamNotSupportedError: If the runtime doesn't support streaming
        """
        async with self.from_context(context) as runtime:
            try:
                async for event in runtime.stream():
                    yield event
            finally:
                for span_processor in self.tracer_span_processors:
                    span_processor.force_flush()

    async def execute_in_root_span(
        self,
        context: C,
        root_span: str = "root",
        attributes: Optional[dict[str, str]] = None,
    ) -> Optional[UiPathRuntimeResult]:
        """Execute runtime with context."""
        async with self.from_context(context) as runtime:
            try:
                tracer: Tracer = trace.get_tracer("uipath-runtime")
                span_attributes = {}
                if context.execution_id:
                    span_attributes["execution.id"] = context.execution_id
                if attributes:
                    span_attributes.update(attributes)

                with tracer.start_as_current_span(
                    root_span,
                    attributes=span_attributes,
                ):
                    return await runtime.execute()
            finally:
                for span_processor in self.tracer_span_processors:
                    span_processor.force_flush()

    async def stream_in_root_span(
        self,
        context: C,
        root_span: str = "root",
        attributes: Optional[dict[str, str]] = None,
    ) -> AsyncGenerator[Union[UiPathRuntimeEvent, UiPathRuntimeResult], None]:
        """Stream runtime execution with context in a root span.

        Args:
            context: The runtime context
            root_span: Name of the root span
            attributes: Optional attributes to add to the span

        Yields:
            UiPathRuntimeEvent instances during execution and final UiPathRuntimeResult

        Raises:
            UiPathRuntimeStreamNotSupportedError: If the runtime doesn't support streaming
        """
        async with self.from_context(context) as runtime:
            try:
                tracer: Tracer = trace.get_tracer("uipath-runtime")
                span_attributes = {}
                if context.execution_id:
                    span_attributes["execution.id"] = context.execution_id
                if attributes:
                    span_attributes.update(attributes)

                with tracer.start_as_current_span(
                    root_span,
                    attributes=span_attributes,
                ):
                    async for event in runtime.stream():
                        yield event
            finally:
                for span_processor in self.tracer_span_processors:
                    span_processor.force_flush()
