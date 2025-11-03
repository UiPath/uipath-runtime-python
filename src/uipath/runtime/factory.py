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
)

from opentelemetry import trace
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor  # type: ignore
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter
from opentelemetry.trace import Tracer
from uipath.core.tracing import UiPathTracingManager

from uipath.runtime.base import UiPathBaseRuntime
from uipath.runtime.context import UiPathRuntimeContext
from uipath.runtime.events import UiPathRuntimeEvent
from uipath.runtime.result import UiPathRuntimeResult
from uipath.runtime.tracing import (
    UiPathExecutionBatchTraceProcessor,
    UiPathExecutionSimpleTraceProcessor,
    UiPathRuntimeExecutionSpanExporter,
)

T = TypeVar("T", bound="UiPathBaseRuntime")


class UiPathRuntimeFactory(Generic[T]):
    """Generic factory for UiPath runtime classes."""

    def __init__(
        self,
        runtime_class: Type[T],
        runtime_generator: Optional[Callable[[UiPathRuntimeContext], T]] = None,
    ):
        """Initialize the UiPathRuntimeFactory."""
        if not issubclass(runtime_class, UiPathBaseRuntime):
            raise TypeError(
                f"runtime_class {runtime_class.__name__} must inherit from UiPathBaseRuntime"
            )

        self.runtime_class = runtime_class
        self.runtime_generator = runtime_generator

    def new_runtime(self, **kwargs) -> T:
        """Create a new runtime instance."""
        context = UiPathRuntimeContext(**kwargs)
        return self.from_context(context)

    def from_context(self, context: UiPathRuntimeContext) -> T:
        """Create runtime instance from context."""
        if self.runtime_generator:
            return self.runtime_generator(context)
        return self.runtime_class(context)


class UiPathRuntimeExecutor:
    """Handles runtime execution with tracing/telemetry."""

    def __init__(self):
        """Initialize the executor."""
        self.tracer_provider: TracerProvider = TracerProvider()
        trace.set_tracer_provider(self.tracer_provider)
        self.tracer_span_processors: List[SpanProcessor] = []
        self.execution_span_exporter = UiPathRuntimeExecutionSpanExporter()
        self.add_span_exporter(self.execution_span_exporter)

    def add_span_exporter(
        self,
        span_exporter: SpanExporter,
        batch: bool = True,
    ) -> "UiPathRuntimeExecutor":
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
    ) -> "UiPathRuntimeExecutor":
        """Add and instrument immediately."""
        instrumentor_class().instrument(tracer_provider=self.tracer_provider)
        UiPathTracingManager.register_current_span_provider(get_current_span_func)
        return self

    async def execute(self, runtime: UiPathBaseRuntime) -> UiPathRuntimeResult:
        """Execute runtime with context."""
        try:
            return await runtime.execute()
        finally:
            self._flush_spans()

    async def stream(
        self, runtime: UiPathBaseRuntime
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
            async for event in runtime.stream():
                yield event
        finally:
            self._flush_spans()

    async def execute_in_root_span(
        self,
        runtime: UiPathBaseRuntime,
        root_span: str = "root",
        attributes: Optional[dict[str, str]] = None,
    ) -> UiPathRuntimeResult:
        """Execute runtime with context in a root span."""
        try:
            tracer: Tracer = trace.get_tracer("uipath-runtime")
            span_attributes = {}
            if runtime.context.execution_id:
                span_attributes["execution.id"] = runtime.context.execution_id
            if attributes:
                span_attributes.update(attributes)

            with tracer.start_as_current_span(
                root_span,
                attributes=span_attributes,
            ):
                return await runtime.execute()
        finally:
            self._flush_spans()

    async def stream_in_root_span(
        self,
        runtime: UiPathBaseRuntime,
        root_span: str = "root",
        attributes: Optional[dict[str, str]] = None,
    ) -> AsyncGenerator[UiPathRuntimeEvent, None]:
        """Stream runtime execution with context in a root span.

        Args:
            runtime: The runtime instance
            context: The runtime context
            root_span: Name of the root span
            attributes: Optional attributes to add to the span

        Yields:
            UiPathRuntimeEvent instances during execution and final UiPathRuntimeResult

        Raises:
            UiPathRuntimeStreamNotSupportedError: If the runtime doesn't support streaming
        """
        try:
            tracer: Tracer = trace.get_tracer("uipath-runtime")
            span_attributes = {}
            if runtime.context.execution_id:
                span_attributes["execution.id"] = runtime.context.execution_id
            if attributes:
                span_attributes.update(attributes)

            with tracer.start_as_current_span(
                root_span,
                attributes=span_attributes,
            ):
                async for event in runtime.stream():
                    yield event
        finally:
            self._flush_spans()

    def get_execution_spans(
        self,
        execution_id: str,
    ) -> List[ReadableSpan]:
        """Retrieve spans for a given execution id."""
        return self.execution_span_exporter.get_spans(execution_id)

    def _flush_spans(self) -> None:
        """Flush all span processors."""
        for span_processor in self.tracer_span_processors:
            span_processor.force_flush()
