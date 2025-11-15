"""Module defining the result for execution suspended at a breakpoint."""

from typing import Any, Literal

from pydantic import Field

from uipath.runtime.result import UiPathRuntimeResult, UiPathRuntimeStatus


class UiPathBreakpointResult(UiPathRuntimeResult):
    """Result for execution suspended at a breakpoint."""

    # Force status to always be SUSPENDED
    status: UiPathRuntimeStatus = Field(
        default=UiPathRuntimeStatus.SUSPENDED, frozen=True
    )
    breakpoint_node: str  # Which node the breakpoint is at
    breakpoint_type: Literal["before", "after"]  # Before or after the node
    current_state: dict[str, Any] | Any  # Current workflow state at breakpoint
    next_nodes: list[str]  # Which node(s) will execute next
