"""Re-export trigger types from uipath.core.triggers for backward compatibility."""

from uipath.core.triggers import (
    UiPathApiTrigger,
    UiPathIntegrationTrigger,
    UiPathResumeTrigger,
    UiPathResumeTriggerName,
    UiPathResumeTriggerType,
)

__all__ = [
    "UiPathApiTrigger",
    "UiPathIntegrationTrigger",
    "UiPathResumeTrigger",
    "UiPathResumeTriggerName",
    "UiPathResumeTriggerType",
]
