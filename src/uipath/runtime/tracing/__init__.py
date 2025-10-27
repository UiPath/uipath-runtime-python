"""OpenTelemetry tracing module with UiPath integration.

This module provides decorators and utilities for instrumenting Python functions
with OpenTelemetry tracing, including custom processors for UiPath execution tracking.
"""

from ._utils import (
    otel_span_to_uipath_span,
)
from .context import UiPathTraceContext
from .decorators import traced
from .manager import UiPathRuntimeTracingManager
from .processors import (
    UiPathExecutionBatchTraceProcessor,
    UiPathExecutionSimpleTraceProcessor,
)
from .span import UiPathRuntimeSpan

__all__ = [
    "traced",
    "UiPathTraceContext",
    "UiPathRuntimeSpan",
    "UiPathRuntimeTracingManager",
    "UiPathExecutionBatchTraceProcessor",
    "UiPathExecutionSimpleTraceProcessor",
    "otel_span_to_uipath_span",
]
