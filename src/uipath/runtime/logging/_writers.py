"""Internal logging writers for stdout/stderr redirection."""

import logging
from typing import TextIO


class LoggerWriter:
    """Redirect stdout/stderr to logging system."""

    def __init__(
        self,
        logger: logging.Logger,
        level: int,
        min_level: int,
        sys_file: TextIO,
    ):
        """Initialize the LoggerWriter."""
        self.logger = logger
        self.level = level
        self.min_level = min_level
        self.buffer = ""
        self.sys_file = sys_file

    def write(self, message: str) -> None:
        """Write message to the logger, buffering until newline."""
        self.buffer += message
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            # Only log if the message is not empty and the level is sufficient
            if line and self.level >= self.min_level:
                # The context variable is automatically available here
                self.logger._log(self.level, line, ())

    def flush(self) -> None:
        """Flush any remaining buffered messages to the logger."""
        # Log any remaining content in the buffer on flush
        if self.buffer and self.level >= self.min_level:
            self.logger._log(self.level, self.buffer, ())
        self.buffer = ""

    def fileno(self) -> int:
        """Get the file descriptor of the original sys.stdout/sys.stderr."""
        try:
            return self.sys_file.fileno()
        except Exception:
            return -1

    def isatty(self) -> bool:
        """Check if the original sys.stdout/sys.stderr is a TTY."""
        return hasattr(self.sys_file, "isatty") and self.sys_file.isatty()

    def writable(self) -> bool:
        """Check if the original sys.stdout/sys.stderr is writable."""
        return True

    def __getattr__(self, name):
        """Delegate attribute access to the original sys.stdout/sys.stderr."""
        return getattr(self.sys_file, name)
