"""Runtime event models for monitoring execution.

Events are emitted during runtime execution to provide visibility into:
- Agent state changes
- Message generation
- Errors and exceptions

All events inherit from UiPathRuntimeEvent and can be filtered by execution_id.
"""

from uipath.runtime.events.base import (
    UiPathRuntimeEvent,
    UiPathRuntimeEventType,
    UiPathRuntimeStatePhase,
)
from uipath.runtime.events.state import (
    UiPathRuntimeMessageEvent,
    UiPathRuntimeStateEvent,
)

__all__ = [
    # Base
    "UiPathRuntimeEvent",
    "UiPathRuntimeEventType",
    "UiPathRuntimeStatePhase",
    # Runtime events
    "UiPathRuntimeStateEvent",
    "UiPathRuntimeMessageEvent",
]
