"""Simple test for runtime factory and executor span capture."""

from typing import Any, AsyncGenerator, TypeVar

import pytest
from opentelemetry import trace
from uipath.core import UiPathTraceManager

from uipath.runtime import (
    UiPathExecuteOptions,
    UiPathExecutionRuntime,
    UiPathRuntimeEvent,
    UiPathRuntimeProtocol,
)
from uipath.runtime.base import UiPathStreamOptions
from uipath.runtime.result import UiPathRuntimeResult, UiPathRuntimeStatus
from uipath.runtime.schema import UiPathRuntimeSchema


class BaseMockRuntime:
    """Base runtime class with unimplemented methods."""

    async def get_schema(self) -> UiPathRuntimeSchema:
        """NotImplemented"""
        raise NotImplementedError()

    async def stream(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathStreamOptions | None = None,
    ) -> AsyncGenerator[UiPathRuntimeEvent, None]:
        """NotImplemented"""
        raise NotImplementedError()
        yield

    async def dispose(self) -> None:
        """Dispose resources if any."""
        pass


class MockRuntimeA(BaseMockRuntime):
    """Mock runtime A for testing."""

    async def execute(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathExecuteOptions | None = None,
    ) -> UiPathRuntimeResult:
        print(f"executing {input}")
        return UiPathRuntimeResult(
            output={"runtime": "A"}, status=UiPathRuntimeStatus.SUCCESSFUL
        )


class MockRuntimeB(BaseMockRuntime):
    """Mock runtime B for testing."""

    async def execute(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathExecuteOptions | None = None,
    ) -> UiPathRuntimeResult:
        print(f"executing {input}")
        return UiPathRuntimeResult(
            output={"runtime": "B"}, status=UiPathRuntimeStatus.SUCCESSFUL
        )


class MockRuntimeC(BaseMockRuntime):
    """Mock runtime C that emits custom spans."""

    async def execute(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathExecuteOptions | None = None,
    ) -> UiPathRuntimeResult:
        print(f"executing {input}")
        tracer = trace.get_tracer("test-runtime-c")

        # Create a child span
        with tracer.start_as_current_span(
            "custom-child-span", attributes={"operation": "child", "step": "1"}
        ):
            # Simulate some work
            pass

        # Create a sibling span
        with tracer.start_as_current_span(
            "custom-sibling-span", attributes={"operation": "sibling", "step": "2"}
        ):
            # Simulate more work
            pass

        # Create nested spans
        with tracer.start_as_current_span(
            "parent-operation", attributes={"operation": "parent"}
        ):
            with tracer.start_as_current_span(
                "nested-child-operation", attributes={"operation": "nested"}
            ):
                pass

        return UiPathRuntimeResult(
            output={"runtime": "C", "spans_created": 4},
            status=UiPathRuntimeStatus.SUCCESSFUL,
        )


T = TypeVar("T", bound=UiPathRuntimeProtocol)


class UiPathTestRuntimeFactory:
    def __init__(self, runtime_class: type[UiPathRuntimeProtocol]):
        self.runtime_class = runtime_class

    async def new_runtime(
        self, entrypoint: str, runtime_id: str
    ) -> UiPathRuntimeProtocol:
        return self.runtime_class()


@pytest.mark.asyncio
async def test_multiple_factories_same_executor():
    """Test factories using same trace manager, verify spans are captured correctly."""
    trace_manager = UiPathTraceManager()

    # Create factories for different runtimes
    factory_a = UiPathTestRuntimeFactory(MockRuntimeA)
    factory_b = UiPathTestRuntimeFactory(MockRuntimeB)
    factory_c = UiPathTestRuntimeFactory(MockRuntimeC)

    # Execute runtime A
    runtime_a = await factory_a.new_runtime(entrypoint="", runtime_id="runtime-a")
    execution_runtime_a = UiPathExecutionRuntime(
        runtime_a, trace_manager, "runtime-a-span", execution_id="exec-a"
    )
    result_a = await execution_runtime_a.execute({"input": "a"})

    # Execute runtime B
    runtime_b = await factory_b.new_runtime(entrypoint="", runtime_id="runtime-b")
    execution_runtime_b = UiPathExecutionRuntime(
        runtime_b, trace_manager, "runtime-b-span", execution_id="exec-b"
    )
    result_b = await execution_runtime_b.execute({"input": "b"})

    # Execute runtime C with custom spans
    runtime_c = await factory_c.new_runtime(entrypoint="", runtime_id="runtime-c")
    execution_runtime_c = UiPathExecutionRuntime(
        runtime_c, trace_manager, "runtime-c-span", execution_id="exec-c"
    )
    result_c = await execution_runtime_c.execute({"input": "c"})

    # Verify results
    assert result_a.status == UiPathRuntimeStatus.SUCCESSFUL
    assert result_a.output == {"runtime": "A"}
    assert result_b.status == UiPathRuntimeStatus.SUCCESSFUL
    assert result_b.output == {"runtime": "B"}
    assert result_c.status == UiPathRuntimeStatus.SUCCESSFUL
    assert result_c.output == {"runtime": "C", "spans_created": 4}

    # Verify spans for execution A
    spans_a = trace_manager.get_execution_spans("exec-a")
    assert len(spans_a) > 0
    span_names_a = [s.name for s in spans_a]
    assert "runtime-a-span" in span_names_a

    # Verify spans for execution B
    spans_b = trace_manager.get_execution_spans("exec-b")
    assert len(spans_b) > 0
    span_names_b = [s.name for s in spans_b]
    assert "runtime-b-span" in span_names_b

    # Verify spans for execution C (should include custom spans)
    spans_c = trace_manager.get_execution_spans("exec-c")
    assert len(spans_c) > 0
    span_names_c = [s.name for s in spans_c]

    # Verify root span exists
    assert "runtime-c-span" in span_names_c

    # Verify custom child and sibling spans exist
    assert "custom-child-span" in span_names_c
    assert "custom-sibling-span" in span_names_c
    assert "parent-operation" in span_names_c
    assert "nested-child-operation" in span_names_c

    # Verify span hierarchy by checking parent relationships
    root_span_c = next(s for s in spans_c if s.name == "runtime-c-span")
    child_span = next(s for s in spans_c if s.name == "custom-child-span")
    sibling_span = next(s for s in spans_c if s.name == "custom-sibling-span")
    parent_op = next(s for s in spans_c if s.name == "parent-operation")
    nested_op = next(s for s in spans_c if s.name == "nested-child-operation")

    # Child and sibling should have root as parent
    assert child_span.parent is not None
    assert sibling_span.parent is not None
    assert child_span.parent.span_id == root_span_c.context.span_id
    assert sibling_span.parent.span_id == root_span_c.context.span_id

    # Nested operation should have parent operation as parent
    assert nested_op.parent is not None
    assert parent_op.parent is not None
    assert nested_op.parent.span_id == parent_op.context.span_id
    assert parent_op.parent.span_id == root_span_c.context.span_id

    # Verify spans are isolated by execution_id
    for span in spans_a:
        assert span.attributes is not None
        assert span.attributes.get("execution.id") == "exec-a"

    for span in spans_b:
        assert span.attributes is not None
        assert span.attributes.get("execution.id") == "exec-b"

    for span in spans_c:
        assert span.attributes is not None
        assert span.attributes.get("execution.id") == "exec-c"

    # Verify logs are captured
    assert execution_runtime_a.log_handler
    assert len(execution_runtime_a.log_handler.buffer) > 0
    assert execution_runtime_a.log_handler.buffer[0].msg == "executing {'input': 'a'}"

    assert execution_runtime_b.log_handler
    assert len(execution_runtime_b.log_handler.buffer) > 0
    assert execution_runtime_b.log_handler.buffer[0].msg == "executing {'input': 'b'}"

    assert execution_runtime_c.log_handler
    assert len(execution_runtime_c.log_handler.buffer) > 0
    assert execution_runtime_c.log_handler.buffer[0].msg == "executing {'input': 'c'}"
