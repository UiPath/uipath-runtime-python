"""OpenTelemetry tracing decorators for function instrumentation."""

import inspect
import json
import logging
from functools import wraps
from typing import Any, Callable, Optional

from opentelemetry import trace
from opentelemetry.trace.status import StatusCode

from uipath.runtime.tracing._utils import (
    format_args_for_trace_json,
    format_object_for_trace_json,
    get_supported_params,
)
from uipath.runtime.tracing.manager import UiPathRuntimeTracingManager

logger = logging.getLogger(__name__)


def _default_input_processor() -> dict[str, Any]:
    """Default input processor that doesn't log any actual input data."""
    return {"redacted": "Input data not logged for privacy/security"}


def _default_output_processor() -> dict[str, Any]:
    """Default output processor that doesn't log any actual output data."""
    return {"redacted": "Output data not logged for privacy/security"}


def _opentelemetry_traced(
    name: Optional[str] = None,
    run_type: Optional[str] = None,
    span_type: Optional[str] = None,
    input_processor: Optional[Callable[..., Any]] = None,
    output_processor: Optional[Callable[..., Any]] = None,
):
    """Default tracer implementation using OpenTelemetry.

    Args:
        name: Optional name for the trace span
        run_type: Optional string to categorize the run type
        span_type: Optional string to categorize the span type
        input_processor: Optional function to process function inputs before recording
        output_processor: Optional function to process function outputs before recording
    """

    def decorator(func):
        trace_name = name or func.__name__
        tracer = trace.get_tracer(__name__)

        # --------- Sync wrapper ---------
        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            ctx = UiPathRuntimeTracingManager.get_parent_context()
            with tracer.start_as_current_span(trace_name, context=ctx) as span:
                span.set_attribute("span_type", span_type or "function_call_sync")
                if run_type is not None:
                    span.set_attribute("run_type", run_type)

                inputs = format_args_for_trace_json(
                    inspect.signature(func), *args, **kwargs
                )
                if input_processor:
                    processed_inputs = input_processor(json.loads(inputs))
                    inputs = json.dumps(processed_inputs, default=str)
                span.set_attribute("input.mime_type", "application/json")
                span.set_attribute("input.value", inputs)

                try:
                    result = func(*args, **kwargs)
                    output = output_processor(result) if output_processor else result
                    span.set_attribute(
                        "output.value", format_object_for_trace_json(output)
                    )
                    span.set_attribute("output.mime_type", "application/json")
                    return result
                except Exception as e:
                    span.record_exception(e)
                    span.set_status(StatusCode.ERROR, str(e))
                    raise

        # --------- Async wrapper ---------
        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            ctx = UiPathRuntimeTracingManager.get_parent_context()
            with tracer.start_as_current_span(trace_name, context=ctx) as span:
                span.set_attribute("span_type", span_type or "function_call_async")
                if run_type is not None:
                    span.set_attribute("run_type", run_type)

                inputs = format_args_for_trace_json(
                    inspect.signature(func), *args, **kwargs
                )
                if input_processor:
                    processed_inputs = input_processor(json.loads(inputs))
                    inputs = json.dumps(processed_inputs, default=str)
                span.set_attribute("input.mime_type", "application/json")
                span.set_attribute("input.value", inputs)

                try:
                    result = await func(*args, **kwargs)
                    output = output_processor(result) if output_processor else result
                    span.set_attribute(
                        "output.value", format_object_for_trace_json(output)
                    )
                    span.set_attribute("output.mime_type", "application/json")
                    return result
                except Exception as e:
                    span.record_exception(e)
                    span.set_status(StatusCode.ERROR, str(e))
                    raise

        # --------- Generator wrapper ---------
        @wraps(func)
        def generator_wrapper(*args, **kwargs):
            ctx = UiPathRuntimeTracingManager.get_parent_context()
            with tracer.start_as_current_span(trace_name, context=ctx) as span:
                span.set_attribute(
                    "span_type", span_type or "function_call_generator_sync"
                )
                if run_type is not None:
                    span.set_attribute("run_type", run_type)

                inputs = format_args_for_trace_json(
                    inspect.signature(func), *args, **kwargs
                )
                if input_processor:
                    processed_inputs = input_processor(json.loads(inputs))
                    inputs = json.dumps(processed_inputs, default=str)
                span.set_attribute("input.mime_type", "application/json")
                span.set_attribute("input.value", inputs)

                try:
                    outputs = []
                    for item in func(*args, **kwargs):
                        outputs.append(item)
                        span.add_event(f"Yielded: {item}")
                        yield item
                    output = output_processor(outputs) if output_processor else outputs
                    span.set_attribute(
                        "output.value", format_object_for_trace_json(output)
                    )
                    span.set_attribute("output.mime_type", "application/json")
                except Exception as e:
                    span.record_exception(e)
                    span.set_status(StatusCode.ERROR, str(e))
                    raise

        # --------- Async generator wrapper ---------
        @wraps(func)
        async def async_generator_wrapper(*args, **kwargs):
            ctx = UiPathRuntimeTracingManager.get_parent_context()
            with tracer.start_as_current_span(trace_name, context=ctx) as span:
                span.set_attribute(
                    "span_type", span_type or "function_call_generator_async"
                )
                if run_type is not None:
                    span.set_attribute("run_type", run_type)

                inputs = format_args_for_trace_json(
                    inspect.signature(func), *args, **kwargs
                )
                if input_processor:
                    processed_inputs = input_processor(json.loads(inputs))
                    inputs = json.dumps(processed_inputs, default=str)
                span.set_attribute("input.mime_type", "application/json")
                span.set_attribute("input.value", inputs)

                try:
                    outputs = []
                    async for item in func(*args, **kwargs):
                        outputs.append(item)
                        span.add_event(f"Yielded: {item}")
                        yield item
                    output = output_processor(outputs) if output_processor else outputs
                    span.set_attribute(
                        "output.value", format_object_for_trace_json(output)
                    )
                    span.set_attribute("output.mime_type", "application/json")
                except Exception as e:
                    span.record_exception(e)
                    span.set_status(StatusCode.ERROR, str(e))
                    raise

        if inspect.iscoroutinefunction(func):
            return async_wrapper
        elif inspect.isgeneratorfunction(func):
            return generator_wrapper
        elif inspect.isasyncgenfunction(func):
            return async_generator_wrapper
        else:
            return sync_wrapper

    return decorator


def traced(
    name: Optional[str] = None,
    run_type: Optional[str] = None,
    span_type: Optional[str] = None,
    input_processor: Optional[Callable[..., Any]] = None,
    output_processor: Optional[Callable[..., Any]] = None,
    hide_input: bool = False,
    hide_output: bool = False,
):
    """Decorator that will trace function invocations.

    Args:
        name: Optional name for the trace span
        run_type: Optional string to categorize the run type
        span_type: Optional string to categorize the span type
        input_processor: Optional function to process function inputs before recording
            Should accept a dictionary of inputs and return a processed dictionary
        output_processor: Optional function to process function outputs before recording
            Should accept the function output and return a processed value
        hide_input: If True, don't log any input data
        hide_output: If True, don't log any output data
    """
    # Apply default processors selectively based on hide flags
    if hide_input:
        input_processor = _default_input_processor
    if hide_output:
        output_processor = _default_output_processor

    # Store the parameters for later reapplication
    params = {
        "name": name,
        "run_type": run_type,
        "span_type": span_type,
        "input_processor": input_processor,
        "output_processor": output_processor,
    }

    # Check for custom implementation first
    tracer_impl = _opentelemetry_traced

    def decorator(func):
        # Check which parameters are supported by the tracer_impl
        supported_params = get_supported_params(tracer_impl, params)

        # Decorate the function with only supported parameters
        decorated_func = tracer_impl(**supported_params)(func)

        # Register both original and decorated function with parameters
        UiPathRuntimeTracingManager.register_traced_function(
            func, decorated_func, params
        )
        return decorated_func

    return decorator


__all__ = ["traced"]
