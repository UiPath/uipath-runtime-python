"""Module defining resume trigger types and data models."""

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


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
