"""Execution context tracking for logging."""

from contextvars import ContextVar
from typing import Optional

# Context variable to track current execution_id
current_execution_id: ContextVar[Optional[str]] = ContextVar(
    "current_execution_id", default=None
)
