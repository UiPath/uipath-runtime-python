"""Main logging interceptor for execution context."""

import io
import logging
import os
import sys
from typing import TextIO, cast

from uipath.runtime.logging._context import current_execution_id
from uipath.runtime.logging._filters import (
    UiPathRuntimeExecutionFilter,
    UiPathRuntimeFilter,
)
from uipath.runtime.logging._writers import LoggerWriter
from uipath.runtime.logging.handlers import (
    UiPathRuntimeFileLogsHandler,
)


class UiPathRuntimeLogsInterceptor:
    """Intercepts all logging and stdout/stderr, routing to either persistent log files or stdout based on whether it's running as a job or not."""

    def __init__(
        self,
        min_level: str | None = "INFO",
        dir: str | None = "__uipath",
        file: str | None = "execution.log",
        job_id: str | None = None,
        execution_id: str | None = None,
        log_handler: logging.Handler | None = None,
    ):
        """Initialize the log interceptor.

        Args:
            min_level: Minimum logging level to capture.
            dir (str): The directory where logs should be stored.
            file (str): The log file name.
            job_id (str, optional): If provided, logs go to file; otherwise, to stdout.
            execution_id (str, optional): Unique identifier for this execution context.
            log_handler (logging.Handler, optional): Custom log handler to use for this execution context.
        """
        min_level = min_level or "INFO"
        self.job_id = job_id
        self.execution_id = execution_id
        self._owns_handler: bool = log_handler is None

        # Convert to numeric level for consistent comparison
        self.numeric_min_level = getattr(logging, min_level.upper(), logging.INFO)

        # Store the original disable level
        self.original_disable_level = logging.root.manager.disable

        self.root_logger = logging.getLogger()
        self.original_level = self.root_logger.level
        self.original_handlers = list(self.root_logger.handlers)

        # Store system stdout/stderr
        self.original_stdout = cast(TextIO, sys.stdout)
        self.original_stderr = cast(TextIO, sys.stderr)

        self.log_handler: (
            UiPathRuntimeFileLogsHandler
            | logging.StreamHandler[TextIO]
            | logging.Handler
        )

        if log_handler:
            self.log_handler = log_handler
        else:
            # Create either file handler (runtime) or stdout handler (debug)
            if not job_id:
                # Only wrap if stdout is using a problematic encoding (like cp1252 on Windows)
                if (
                    hasattr(sys.stdout, "encoding")
                    and hasattr(sys.stdout, "buffer")
                    and sys.stdout.encoding
                    and sys.stdout.encoding.lower() not in ("utf-8", "utf8")
                ):
                    # Wrap stdout with UTF-8 encoding for the handler
                    self.utf8_stdout = io.TextIOWrapper(
                        sys.stdout.buffer,
                        encoding="utf-8",
                        errors="replace",
                        line_buffering=True,
                    )
                    self.log_handler = logging.StreamHandler(self.utf8_stdout)
                else:
                    # stdout already has good encoding, use it directly
                    self.log_handler = logging.StreamHandler(sys.stdout)

                formatter = logging.Formatter("%(message)s")
                self.log_handler.setFormatter(formatter)
            else:
                # Ensure directory exists for file logging
                dir = dir or "__uipath"
                file = file or "execution.log"
                os.makedirs(dir, exist_ok=True)
                log_file = os.path.join(dir, file)
                self.log_handler = UiPathRuntimeFileLogsHandler(file=log_file)

        self.log_handler.setLevel(self.numeric_min_level)

        # Add execution context filter if execution_id provided
        self.execution_filter: logging.Filter | None = None
        if execution_id:
            self.execution_filter = UiPathRuntimeExecutionFilter(execution_id)
            self.log_handler.addFilter(self.execution_filter)
        else:
            # Main logging: filter out child execution logs
            self.execution_filter = UiPathRuntimeFilter()
            self.log_handler.addFilter(self.execution_filter)

        self.logger = logging.getLogger("runtime")
        self.patched_loggers: set[str] = set()

    def _clean_all_handlers(self, logger: logging.Logger) -> None:
        """Remove ALL handlers from a logger except ours."""
        handlers_to_remove = list(logger.handlers)
        for handler in handlers_to_remove:
            logger.removeHandler(handler)

        # Now add our handler
        logger.addHandler(self.log_handler)

    def setup(self) -> None:
        """Configure logging to use our persistent handler."""
        # Set the context variable for this execution
        if self.execution_id:
            current_execution_id.set(self.execution_id)

        # Only use global disable if we're not in a parallel execution context
        if not self.execution_id and self.numeric_min_level > logging.NOTSET:
            logging.disable(self.numeric_min_level - 1)

        # Set root logger level
        self.root_logger.setLevel(self.numeric_min_level)

        if self.execution_id:
            # Child execution mode: add our handler without removing others
            if self.log_handler not in self.root_logger.handlers:
                self.root_logger.addHandler(self.log_handler)

            # Keep propagation enabled so logs flow through filters
            # Our ExecutionContextFilter will ensure only our logs get through our handler
            for logger_name in logging.root.manager.loggerDict:
                logger = logging.getLogger(logger_name)
                # Keep propagation enabled for filtering to work
                # logger.propagate remains True (default)
                self.patched_loggers.add(logger_name)

            # Child executions should redirect stdout/stderr to their own handler
            # This ensures print statements are captured per execution
            self._redirect_stdout_stderr()
        else:
            # Master execution mode: remove all handlers and add only ours
            self._clean_all_handlers(self.root_logger)

            # Set up propagation for all existing loggers
            for logger_name in logging.root.manager.loggerDict:
                logger = logging.getLogger(logger_name)
                logger.propagate = False  # Prevent double-logging
                self._clean_all_handlers(logger)
                self.patched_loggers.add(logger_name)

            # Master redirects stdout/stderr
            self._redirect_stdout_stderr()

    def _redirect_stdout_stderr(self) -> None:
        """Redirect stdout and stderr to the logging system."""
        # Set up stdout and stderr loggers
        stdout_logger = logging.getLogger("stdout")
        stderr_logger = logging.getLogger("stderr")

        if self.execution_id:
            # Child execution: add our handler to stdout/stderr loggers
            stdout_logger.propagate = False
            stderr_logger.propagate = False

            if self.log_handler not in stdout_logger.handlers:
                stdout_logger.addHandler(self.log_handler)
            if self.log_handler not in stderr_logger.handlers:
                stderr_logger.addHandler(self.log_handler)
        else:
            # Master execution: clean and set up handlers
            stdout_logger.propagate = False
            stderr_logger.propagate = False

            self._clean_all_handlers(stdout_logger)
            self._clean_all_handlers(stderr_logger)

        # Use the min_level in the LoggerWriter to filter messages
        sys.stdout = LoggerWriter(
            stdout_logger, logging.INFO, self.numeric_min_level, self.original_stdout
        )
        sys.stderr = LoggerWriter(
            stderr_logger, logging.ERROR, self.numeric_min_level, self.original_stderr
        )

    def teardown(self) -> None:
        """Restore original logging configuration."""
        # Clear the context variable
        if self.execution_id:
            current_execution_id.set(None)

        # Restore the original disable level
        if not self.execution_id:
            logging.disable(self.original_disable_level)

        # Remove our handler and filter
        if self.execution_filter:
            self.log_handler.removeFilter(self.execution_filter)

        if self.log_handler in self.root_logger.handlers:
            self.root_logger.removeHandler(self.log_handler)

        # Remove from stdout/stderr loggers
        stdout_logger = logging.getLogger("stdout")
        stderr_logger = logging.getLogger("stderr")
        if self.log_handler in stdout_logger.handlers:
            stdout_logger.removeHandler(self.log_handler)
        if self.log_handler in stderr_logger.handlers:
            stderr_logger.removeHandler(self.log_handler)

        if not self.execution_id:
            # Master execution: restore everything
            for logger_name in self.patched_loggers:
                logger = logging.getLogger(logger_name)
                if self.log_handler in logger.handlers:
                    logger.removeHandler(self.log_handler)

            self.root_logger.setLevel(self.original_level)
            for handler in self.original_handlers:
                if handler not in self.root_logger.handlers:
                    self.root_logger.addHandler(handler)

        if self._owns_handler:
            self.log_handler.close()

        if hasattr(self, "utf8_stdout"):
            self.utf8_stdout.close()

        # Only restore streams if we redirected them
        if self.original_stdout and self.original_stderr:
            sys.stdout = self.original_stdout
            sys.stderr = self.original_stderr

    def __enter__(self):
        """Enter the logging interceptor context."""
        self.setup()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit the logging interceptor context."""
        if exc_type is not None:
            self.logger.error(
                f"Exception occurred: {exc_val}", exc_info=(exc_type, exc_val, exc_tb)
            )
        self.teardown()
        return False
