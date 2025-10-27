"""Trace context information for tracing and debugging."""

from typing import Optional, Union

from pydantic import BaseModel


class UiPathTraceContext(BaseModel):
    """Trace context information for tracing and debugging."""

    trace_id: Optional[str] = None
    parent_span_id: Optional[str] = None
    root_span_id: Optional[str] = None
    org_id: Optional[str] = None
    tenant_id: Optional[str] = None
    job_id: Optional[str] = None
    folder_key: Optional[str] = None
    process_key: Optional[str] = None
    enabled: Union[bool, str] = False
    reference_id: Optional[str] = None


__all__ = ["UiPathTraceContext"]
