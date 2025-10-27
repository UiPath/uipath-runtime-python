"""Logging filters for execution context."""

import logging

from uipath.runtime.logging._context import current_execution_id


class UiPathRuntimeExecutionFilter(logging.Filter):
    """Filter that only allows logs from a specific child execution context."""

    def __init__(self, execution_id: str):
        """Initialize the filter with the target execution_id."""
        super().__init__()
        self.execution_id = execution_id

    def filter(self, record: logging.LogRecord) -> bool:
        """Allow logs that have matching execution_id attribute or context."""
        # First check if record has execution_id attribute
        record_execution_id = getattr(record, "execution_id", None)
        if record_execution_id == self.execution_id:
            return True

        # Fall back to context variable
        ctx_execution_id = current_execution_id.get()
        if ctx_execution_id == self.execution_id:
            # Inject execution_id into record for downstream handlers
            record.execution_id = self.execution_id
            return True

        return False


class UiPathRuntimeFilter(logging.Filter):
    """Filter for master handler that blocks logs from any child execution."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Block logs that belong to a child execution context."""
        ctx_execution_id = current_execution_id.get()
        # Block if there's an active child execution context
        return ctx_execution_id is None
