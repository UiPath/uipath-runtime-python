"""Per-execution slots for a log handler and a result sink a caller can install.

Unset, the runtime writes its usual files; set, it routes a job's logs and result to them instead.
Values are contextvars, isolated across concurrent jobs in one process.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Any, Callable

# (result, output_arguments_file_path) -> None
ResultSink = Callable[[Any, str], None]

_log_handler: ContextVar[logging.Handler | None] = ContextVar(
    "uipath_log_handler", default=None
)
_result_sink: ContextVar[ResultSink | None] = ContextVar(
    "uipath_result_sink", default=None
)


def set_log_handler(handler: logging.Handler | None) -> None:
    """Install the log handler (``None`` clears)."""
    _log_handler.set(handler)


def get_log_handler() -> logging.Handler | None:
    """The installed log handler, or None."""
    return _log_handler.get()


def set_result_sink(sink: ResultSink | None) -> None:
    """Install the result sink (``None`` clears)."""
    _result_sink.set(sink)


def get_result_sink() -> ResultSink | None:
    """The installed result sink, or None."""
    return _result_sink.get()
