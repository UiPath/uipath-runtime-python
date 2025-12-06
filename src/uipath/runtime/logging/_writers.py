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
        self._in_logging = False  # Recursion guard

    def write(self, message: str) -> None:
        """Write message to the logger, buffering until newline."""
        # Prevent infinite recursion when logging.handleError writes to stderr
        if self._in_logging:
            if self.sys_file:
                try:
                    self.sys_file.write(message)
                except (OSError, IOError):
                    pass  # Fail silently if we can't write
            return

        try:
            self._in_logging = True
            self.buffer += message
            while "\n" in self.buffer:
                line, self.buffer = self.buffer.split("\n", 1)
                # Only log if the message is not empty and the level is sufficient
                if line and self.level >= self.min_level:
                    self.logger._log(self.level, line, ())
        finally:
            self._in_logging = False

    def flush(self) -> None:
        """Flush any remaining buffered messages to the logger."""
        if self._in_logging:
            if self.sys_file:
                try:
                    self.sys_file.flush()
                except (OSError, IOError):
                    pass  # Fail silently if we can't flush
            return

        try:
            self._in_logging = True
            # Log any remaining content in the buffer on flush
            if self.buffer and self.level >= self.min_level:
                self.logger._log(self.level, self.buffer, ())
            self.buffer = ""
        finally:
            self._in_logging = False

    def fileno(self) -> int:
        """Get the file descriptor of the original sys.stdout/sys.stderr."""
        try:
            return self.sys_file.fileno()
        except Exception:
            return -1

    def isatty(self) -> bool:
        """Check if the original sys.stdout/sys.stderr is a TTY."""
        try:
            return hasattr(self.sys_file, "isatty") and self.sys_file.isatty()
        except (AttributeError, OSError, ValueError):
            return False

    def writable(self) -> bool:
        """Check if the original sys.stdout/sys.stderr is writable."""
        return True

    def __getattr__(self, name):
        """Delegate attribute access to the original sys.stdout/sys.stderr."""
        return getattr(self.sys_file, name)
