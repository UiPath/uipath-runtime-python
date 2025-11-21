"""Result of an execution with status and optional error information."""

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from uipath.runtime.errors import UiPathErrorContract
from uipath.runtime.events import UiPathRuntimeEvent, UiPathRuntimeEventType
from uipath.runtime.resumable.trigger import UiPathResumeTrigger


class UiPathRuntimeStatus(str, Enum):
    """Standard status values for runtime execution."""

    SUCCESSFUL = "successful"
    FAULTED = "faulted"
    SUSPENDED = "suspended"


class UiPathRuntimeResult(UiPathRuntimeEvent):
    """Result of an execution with status and optional error information."""

    output: dict[str, Any] | BaseModel | None = None
    status: UiPathRuntimeStatus = UiPathRuntimeStatus.SUCCESSFUL
    trigger: UiPathResumeTrigger | None = None
    error: UiPathErrorContract | None = None

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

        if self.trigger:
            result["resume"] = self.trigger.model_dump(by_alias=True)

        if self.error:
            result["error"] = self.error.model_dump()

        return result
