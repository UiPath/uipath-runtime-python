from typing import Any, AsyncGenerator

import pytest

from uipath.runtime.base import UiPathStreamOptions
from uipath.runtime.events import (
    UiPathRuntimeEvent,
    UiPathRuntimeStateEvent,
    UiPathRuntimeStatePhase,
)
from uipath.runtime.result import UiPathRuntimeResult, UiPathRuntimeStatus


def test_state_event_phase_defaults_to_updated() -> None:
    """Phase should default to UPDATED for backward compatibility."""
    event = UiPathRuntimeStateEvent(payload={"messages": []})

    assert event.phase == UiPathRuntimeStatePhase.UPDATED


def test_state_event_phase_can_be_set_explicitly() -> None:
    """Phase should accept any valid UiPathRuntimeStatePhase value."""
    for phase in UiPathRuntimeStatePhase:
        event = UiPathRuntimeStateEvent(payload={}, phase=phase)
        assert event.phase == phase


class PhaseAwareMockRuntime:
    """Mock runtime that emits started/updated/completed state events per node."""

    def __init__(self, nodes: list[str], *, failing_node: str | None = None) -> None:
        self.nodes = nodes
        self.failing_node = failing_node

    async def stream(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathStreamOptions | None = None,
    ) -> AsyncGenerator[UiPathRuntimeEvent, None]:
        for node in self.nodes:
            yield UiPathRuntimeStateEvent(
                node_name=node,
                payload={},
                phase=UiPathRuntimeStatePhase.STARTED,
            )

            if node == self.failing_node:
                yield UiPathRuntimeStateEvent(
                    node_name=node,
                    payload={"error": f"{node} failed"},
                    phase=UiPathRuntimeStatePhase.FAULTED,
                )
                yield UiPathRuntimeResult(
                    status=UiPathRuntimeStatus.FAULTED,
                    output={"error": f"{node} failed"},
                )
                return

            yield UiPathRuntimeStateEvent(
                node_name=node,
                payload={"key": f"{node}_value"},
                phase=UiPathRuntimeStatePhase.UPDATED,
            )
            yield UiPathRuntimeStateEvent(
                node_name=node,
                payload={"key": f"{node}_value"},
                phase=UiPathRuntimeStatePhase.COMPLETED,
            )

        yield UiPathRuntimeResult(
            status=UiPathRuntimeStatus.SUCCESSFUL,
            output={"done": True},
        )


@pytest.mark.asyncio
async def test_runtime_stream_emits_phase_lifecycle() -> None:
    """A runtime streaming started/updated/completed phases per node."""
    runtime = PhaseAwareMockRuntime(nodes=["planner", "executor"])

    state_events: list[UiPathRuntimeStateEvent] = []
    result: UiPathRuntimeResult | None = None

    async for event in runtime.stream({}):
        if isinstance(event, UiPathRuntimeResult):
            result = event
        elif isinstance(event, UiPathRuntimeStateEvent):
            state_events.append(event)

    # 2 nodes x 3 phases = 6 state events
    assert len(state_events) == 6

    # Verify per-node lifecycle ordering
    for i, node in enumerate(["planner", "executor"]):
        group = state_events[i * 3 : i * 3 + 3]
        assert group[0].node_name == node
        assert group[0].phase == UiPathRuntimeStatePhase.STARTED
        assert group[0].payload == {}

        assert group[1].node_name == node
        assert group[1].phase == UiPathRuntimeStatePhase.UPDATED
        assert group[1].payload == {"key": f"{node}_value"}

        assert group[2].node_name == node
        assert group[2].phase == UiPathRuntimeStatePhase.COMPLETED
        assert group[2].payload == {"key": f"{node}_value"}

    # Final result
    assert result is not None
    assert result.status == UiPathRuntimeStatus.SUCCESSFUL
    assert result.output == {"done": True}


@pytest.mark.asyncio
async def test_runtime_stream_emits_faulted_phase_on_error() -> None:
    """A node that fails should emit started then faulted, and stop the stream."""
    runtime = PhaseAwareMockRuntime(
        nodes=["planner", "executor"], failing_node="executor"
    )

    state_events: list[UiPathRuntimeStateEvent] = []
    result: UiPathRuntimeResult | None = None

    async for event in runtime.stream({}):
        if isinstance(event, UiPathRuntimeResult):
            result = event
        elif isinstance(event, UiPathRuntimeStateEvent):
            state_events.append(event)

    # planner: started, updated, completed (3) + executor: started, faulted (2) = 5
    assert len(state_events) == 5

    # planner completed normally
    assert state_events[0].phase == UiPathRuntimeStatePhase.STARTED
    assert state_events[1].phase == UiPathRuntimeStatePhase.UPDATED
    assert state_events[2].phase == UiPathRuntimeStatePhase.COMPLETED

    # executor started then faulted
    assert state_events[3].node_name == "executor"
    assert state_events[3].phase == UiPathRuntimeStatePhase.STARTED
    assert state_events[4].node_name == "executor"
    assert state_events[4].phase == UiPathRuntimeStatePhase.FAULTED
    assert state_events[4].payload == {"error": "executor failed"}

    # Result is faulted
    assert result is not None
    assert result.status == UiPathRuntimeStatus.FAULTED
