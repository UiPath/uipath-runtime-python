"""Re-export trigger types from uipath.core.triggers for backward compatibility."""

from uipath.core.triggers import (
    UiPathApiTrigger,
    UiPathResumeTrigger,
    UiPathResumeTriggerName,
    UiPathResumeTriggerType,
)

__all__ = [
    "UiPathApiTrigger",
    "UiPathResumeTrigger",
    "UiPathResumeTriggerName",
    "UiPathResumeTriggerType",
]
