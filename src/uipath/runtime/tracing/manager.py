"""Tracing manager for handling tracer implementations and function registry."""

import logging
from typing import Any, Callable, List, Optional, Tuple

from opentelemetry import context, trace
from opentelemetry.trace import set_span_in_context

logger = logging.getLogger(__name__)


class UiPathRuntimeTracingManager:
    """Static utility class to manage tracing implementations and decorated functions."""

    # Registry to track original functions, decorated functions, and their parameters
    # Each entry is (original_func, decorated_func, params)
    _traced_registry: List[Tuple[Callable[..., Any], Callable[..., Any], Any]] = []

    _current_span_provider: Optional[Callable[[], Any]] = None

    @classmethod
    def register_current_span_provider(
        cls, current_span_provider: Optional[Callable[[], Any]]
    ):
        """Register a custom current span provider function.

        Args:
            current_span_provider: A function that returns the current span from an external
                                 tracing framework. If None, no custom span parenting will be used.
        """
        cls._current_span_provider = current_span_provider

    @staticmethod
    def get_parent_context():
        """Get the parent context for span creation.

        Prioritizes:
        1. Currently active OTel span (for recursion/children)
        2. External span provider (for top-level calls)
        3. Current context as fallback
        """
        # Always use the currently active OTel span if valid (recursion / children)
        current_span = trace.get_current_span()
        if current_span is not None and current_span.get_span_context().is_valid:
            return set_span_in_context(current_span)

        # Only for the very top-level call, fallback to external span provider
        if UiPathRuntimeTracingManager._current_span_provider is not None:
            try:
                external_span = UiPathRuntimeTracingManager._current_span_provider()
                if external_span is not None:
                    return set_span_in_context(external_span)
            except Exception as e:
                logger.warning(f"Error getting current span from provider: {e}")

        # Last fallback
        return context.get_current()

    @classmethod
    def register_traced_function(cls, original_func, decorated_func, params):
        """Register a function decorated with @traced and its parameters.

        Args:
            original_func: The original function before decoration
            decorated_func: The function after decoration
            params: The parameters used for tracing
        """
        cls._traced_registry.append((original_func, decorated_func, params))


__all__ = ["UiPathRuntimeTracingManager"]
