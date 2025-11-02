"""OpenTelemetry tracing module with UiPath integration.

This module provides decorators and utilities for instrumenting Python functions
with OpenTelemetry tracing, including custom processors for UiPath execution tracking.
"""

from uipath.runtime.tracing.exporters import UiPathRuntimeExecutionSpanExporter
from uipath.runtime.tracing.processors import (
    UiPathExecutionBatchTraceProcessor,
    UiPathExecutionSimpleTraceProcessor,
)

__all__ = [
    "UiPathExecutionBatchTraceProcessor",
    "UiPathExecutionSimpleTraceProcessor",
    "UiPathRuntimeExecutionSpanExporter",
]
