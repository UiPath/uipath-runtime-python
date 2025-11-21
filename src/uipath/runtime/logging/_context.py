"""Execution context tracking for logging."""

from contextvars import ContextVar

# Context variable to track current execution_id
current_execution_id: ContextVar[str | None] = ContextVar(
    "current_execution_id", default=None
)
