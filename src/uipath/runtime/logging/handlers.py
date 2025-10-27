"""Custom logging handlers for execution-based logging."""

import logging


class UiPathRuntimeExecutionLogHandler(logging.Handler):
    """Handler for an execution unit."""

    def __init__(self, execution_id: str):
        """Initialize the buffered handler."""
        super().__init__()
        self.execution_id: str = execution_id
        self.buffer: list[logging.LogRecord] = []
        self.setFormatter(logging.Formatter("[%(asctime)s][%(levelname)s] %(message)s"))

    def emit(self, record: logging.LogRecord):
        """Store log record in buffer."""
        self.buffer.append(record)


class UiPathRuntimeFileLogsHandler(logging.FileHandler):
    """A simple log handler that always writes to a single file without rotation."""

    def __init__(self, file: str):
        """Initialize the handler to write logs to a single file, appending always.

        Args:
            file (str): The file where logs should be stored.
        """
        # Open file in append mode ('a'), so logs are not overwritten
        super().__init__(file, mode="a", encoding="utf8")

        self.formatter = logging.Formatter("[%(asctime)s][%(levelname)s] %(message)s")
        self.setFormatter(self.formatter)


__all__ = ["UiPathRuntimeExecutionLogHandler", "UiPathRuntimeFileLogsHandler"]
