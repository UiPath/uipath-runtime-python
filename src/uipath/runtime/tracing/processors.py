"""Custom span processors for UiPath execution tracing."""

from typing import Optional, cast

from opentelemetry import context as context_api
from opentelemetry import trace
from opentelemetry.sdk.trace import Span
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
)


class UiPathExecutionTraceProcessorMixin:
    def on_start(
        self, span: Span, parent_context: Optional[context_api.Context] = None
    ):
        """Called when a span is started."""
        parent_span: Optional[Span]
        if parent_context:
            parent_span = cast(Span, trace.get_current_span(parent_context))
        else:
            parent_span = cast(Span, trace.get_current_span())

        if parent_span and parent_span.is_recording() and parent_span.attributes:
            execution_id = parent_span.attributes.get("execution.id")
            if execution_id:
                span.set_attribute("execution.id", execution_id)


class UiPathExecutionBatchTraceProcessor(
    UiPathExecutionTraceProcessorMixin, BatchSpanProcessor
):
    """Batch span processor that propagates execution.id."""


class UiPathExecutionSimpleTraceProcessor(
    UiPathExecutionTraceProcessorMixin, SimpleSpanProcessor
):
    """Simple span processor that propagates execution.id."""


__all__ = [
    "UiPathExecutionBatchTraceProcessor",
    "UiPathExecutionSimpleTraceProcessor",
]
