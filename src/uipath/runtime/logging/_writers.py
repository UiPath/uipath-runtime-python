"""Internal logging writers for stdout/stderr redirection."""

import logging
from typing import TextIO

from uipath.runtime.logging._context import current_execution_id


class LoggerWriter:
    """Redirect stdout/stderr to logging system.

    Maintains per-execution-context buffers so that concurrent async tasks
    (e.g. parallel eval runs) do not interleave partial lines.
    """

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
        self._buffers: dict[str | None, str] = {}
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
            ctx = current_execution_id.get()
            buf = self._buffers.get(ctx, "") + message
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                # Only log if the message is not empty and the level is sufficient
                if line and self.level >= self.min_level:
                    self.logger._log(self.level, line, ())
            if buf:
                self._buffers[ctx] = buf
            else:
                self._buffers.pop(ctx, None)
        finally:
            self._in_logging = False

    def flush(self) -> None:
        """Flush the current execution context's buffered messages to the logger."""
        if self._in_logging:
            if self.sys_file:
                try:
                    self.sys_file.flush()
                except (OSError, IOError):
                    pass  # Fail silently if we can't flush
            return

        try:
            self._in_logging = True
            ctx = current_execution_id.get()
            buf = self._buffers.pop(ctx, "")
            if buf and self.level >= self.min_level:
                self.logger._log(self.level, buf, ())
        finally:
            self._in_logging = False

    def flush_all(self) -> None:
        """Flush all execution contexts' buffered messages. Called by master teardown."""
        if self._in_logging:
            return

        try:
            self._in_logging = True
            for buf in self._buffers.values():
                if buf and self.level >= self.min_level:
                    self.logger._log(self.level, buf, ())
            self._buffers.clear()
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
