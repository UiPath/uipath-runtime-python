"""Result of an execution with status and optional error information."""

from enum import Enum
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, Field

from uipath.runtime.errors import UiPathErrorContract
from uipath.runtime.events import UiPathRuntimeEvent, UiPathRuntimeEventType


class UiPathRuntimeStatus(str, Enum):
    """Standard status values for runtime execution."""

    SUCCESSFUL = "successful"
    FAULTED = "faulted"
    SUSPENDED = "suspended"


class UiPathResumeTriggerType(str, Enum):
    """Constants representing different types of resume job triggers in the system."""

    NONE = "None"
    QUEUE_ITEM = "QueueItem"
    JOB = "Job"
    ACTION = "Task"
    TIMER = "Timer"
    INBOX = "Inbox"
    API = "Api"


class UiPathApiTrigger(BaseModel):
    """API resume trigger request."""

    inbox_id: Optional[str] = Field(default=None, alias="inboxId")
    request: Any = None

    model_config = {"populate_by_name": True}


class UiPathResumeTrigger(BaseModel):
    """Information needed to resume execution."""

    trigger_type: UiPathResumeTriggerType = Field(
        default=UiPathResumeTriggerType.API, alias="triggerType"
    )
    item_key: Optional[str] = Field(default=None, alias="itemKey")
    api_resume: Optional[UiPathApiTrigger] = Field(default=None, alias="apiResume")
    folder_path: Optional[str] = Field(default=None, alias="folderPath")
    folder_key: Optional[str] = Field(default=None, alias="folderKey")
    payload: Optional[Any] = Field(default=None, alias="interruptObject")

    model_config = {"populate_by_name": True}


class UiPathRuntimeResult(UiPathRuntimeEvent):
    """Result of an execution with status and optional error information."""

    output: Optional[Union[dict[str, Any], BaseModel]] = None
    status: UiPathRuntimeStatus = UiPathRuntimeStatus.SUCCESSFUL
    resume: Optional[UiPathResumeTrigger] = None
    error: Optional[UiPathErrorContract] = None

    event_type: UiPathRuntimeEventType = Field(
        default=UiPathRuntimeEventType.RUNTIME_RESULT, frozen=True
    )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary format for output."""
        if self.output is None:
            output_data = {}
        elif isinstance(self.output, BaseModel):
            output_data = self.output.model_dump()
        else:
            output_data = self.output

        result = {
            "output": output_data,
            "status": self.status,
        }

        if self.resume:
            result["resume"] = self.resume.model_dump(by_alias=True)

        if self.error:
            result["error"] = self.error.model_dump()

        return result


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
