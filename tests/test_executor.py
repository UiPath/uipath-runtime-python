"""Simple test for runtime factory and executor span capture."""

import pytest
from opentelemetry import trace

from uipath.runtime.base import UiPathBaseRuntime
from uipath.runtime.context import UiPathRuntimeContext
from uipath.runtime.factory import UiPathRuntimeExecutor, UiPathRuntimeFactory
from uipath.runtime.result import UiPathRuntimeResult, UiPathRuntimeStatus


class MockRuntimeA(UiPathBaseRuntime):
    """Mock runtime A for testing."""

    async def validate(self):
        pass

    async def cleanup(self):
        pass

    async def execute(self) -> UiPathRuntimeResult:
        return UiPathRuntimeResult(
            output={"runtime": "A"}, status=UiPathRuntimeStatus.SUCCESSFUL
        )


class MockRuntimeB(UiPathBaseRuntime):
    """Mock runtime B for testing."""

    async def validate(self):
        pass

    async def cleanup(self):
        pass

    async def execute(self) -> UiPathRuntimeResult:
        return UiPathRuntimeResult(
            output={"runtime": "B"}, status=UiPathRuntimeStatus.SUCCESSFUL
        )


class MockRuntimeC(UiPathBaseRuntime):
    """Mock runtime C that emits custom spans."""

    async def validate(self):
        pass

    async def cleanup(self):
        pass

    async def execute(self) -> UiPathRuntimeResult:
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


@pytest.mark.asyncio
async def test_multiple_factories_same_executor():
    """Test two factories using same executor, verify spans are captured correctly."""

    # Create two factories for different runtimes
    factory_a = UiPathRuntimeFactory(MockRuntimeA)
    factory_b = UiPathRuntimeFactory(MockRuntimeB)
    factory_c = UiPathRuntimeFactory(MockRuntimeC)

    # Create single executor
    executor = UiPathRuntimeExecutor()

    # Execute runtime A
    runtime_a = factory_a.from_context(UiPathRuntimeContext())
    async with runtime_a:
        result_a = await executor.execute_in_root_span(
            runtime_a, execution_id="exec-a", root_span="runtime-a-span"
        )

    # Execute runtime B
    runtime_b = factory_b.from_context(UiPathRuntimeContext())
    async with runtime_b:
        result_b = await executor.execute_in_root_span(
            runtime_b, execution_id="exec-b", root_span="runtime-b-span"
        )

    # Execute runtime C with custom spans
    runtime_c = factory_c.from_context(UiPathRuntimeContext())
    async with runtime_c:
        result_c = await executor.execute_in_root_span(
            runtime_c, execution_id="exec-c", root_span="runtime-c-span"
        )

    # Verify results
    assert result_a.status == UiPathRuntimeStatus.SUCCESSFUL
    assert result_a.output == {"runtime": "A"}
    assert result_b.status == UiPathRuntimeStatus.SUCCESSFUL
    assert result_b.output == {"runtime": "B"}
    assert result_c.status == UiPathRuntimeStatus.SUCCESSFUL
    assert result_c.output == {"runtime": "C", "spans_created": 4}

    # Verify spans for execution A
    spans_a = executor.get_execution_spans("exec-a")
    assert len(spans_a) > 0
    span_names_a = [s.name for s in spans_a]
    assert "runtime-a-span" in span_names_a

    # Verify spans for execution B
    spans_b = executor.get_execution_spans("exec-b")
    assert len(spans_b) > 0
    span_names_b = [s.name for s in spans_b]
    assert "runtime-b-span" in span_names_b

    # Verify spans for execution C (should include custom spans)
    spans_c = executor.get_execution_spans("exec-c")
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
