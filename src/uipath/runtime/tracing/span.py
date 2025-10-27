"""Defines the UiPathSpan model for tracing spans in UiPath."""

from datetime import datetime
from os import environ as env
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class UiPathRuntimeSpan(BaseModel):
    """Represents a span in the UiPath tracing system."""

    model_config = ConfigDict(populate_by_name=True)

    id: UUID = Field(serialization_alias="Id")
    trace_id: UUID = Field(serialization_alias="TraceId")
    name: str = Field(serialization_alias="Name")
    attributes: str = Field(serialization_alias="Attributes")
    parent_id: Optional[UUID] = Field(None, serialization_alias="ParentId")
    start_time: str = Field(
        default_factory=lambda: datetime.now().isoformat(),
        serialization_alias="StartTime",
    )
    end_time: str = Field(
        default_factory=lambda: datetime.now().isoformat(),
        serialization_alias="EndTime",
    )
    status: int = Field(1, serialization_alias="Status")
    created_at: str = Field(
        default_factory=lambda: datetime.now().isoformat() + "Z",
        serialization_alias="CreatedAt",
    )
    updated_at: str = Field(
        default_factory=lambda: datetime.now().isoformat() + "Z",
        serialization_alias="UpdatedAt",
    )
    organization_id: Optional[str] = Field(
        default_factory=lambda: env.get("UIPATH_ORGANIZATION_ID", ""),
        serialization_alias="OrganizationId",
    )
    tenant_id: Optional[str] = Field(
        default_factory=lambda: env.get("UIPATH_TENANT_ID", ""),
        serialization_alias="TenantId",
    )
    expiry_time_utc: Optional[str] = Field(None, serialization_alias="ExpiryTimeUtc")
    folder_key: Optional[str] = Field(
        default_factory=lambda: env.get("UIPATH_FOLDER_KEY", ""),
        serialization_alias="FolderKey",
    )
    source: Optional[str] = Field(None, serialization_alias="Source")
    span_type: str = Field("Coded Agents", serialization_alias="SpanType")
    process_key: Optional[str] = Field(
        default_factory=lambda: env.get("UIPATH_PROCESS_UUID"),
        serialization_alias="ProcessKey",
    )
    reference_id: Optional[str] = Field(
        default_factory=lambda: env.get("TRACE_REFERENCE_ID"),
        serialization_alias="ReferenceId",
    )
    job_key: Optional[str] = Field(
        default_factory=lambda: env.get("UIPATH_JOB_KEY"), serialization_alias="JobKey"
    )

    def to_dict(self) -> dict[str, Any]:
        """Convert the Span to a dictionary suitable for JSON serialization.

        Returns a dict with PascalCase keys for UiPath API compatibility.
        """
        return self.model_dump(by_alias=True, exclude_none=False, mode="json")


__all__ = ["UiPathRuntimeSpan"]
