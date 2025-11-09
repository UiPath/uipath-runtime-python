"""Base classes for UiPath runtime events."""

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class UiPathRuntimeEventType(str, Enum):
    """Types of events that can be emitted during execution."""

    RUNTIME_MESSAGE = "runtime_message"
    RUNTIME_STATE = "runtime_state"
    RUNTIME_ERROR = "runtime_error"
    RUNTIME_RESULT = "runtime_result"


class UiPathRuntimeEvent(BaseModel):
    """Base class for all UiPath runtime events."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    event_type: UiPathRuntimeEventType
    execution_id: Optional[str] = Field(
        default=None, description="The runtime execution id associated with the event"
    )
    metadata: Optional[dict[str, Any]] = Field(
        default=None, description="Additional event context"
    )


__all__ = ["UiPathRuntimeEventType", "UiPathRuntimeEvent"]
