"""Job-API IPC: stream job logs back to the handler (per-job client or pooled callback)."""

from uipath.runtime.jobapi.client import IpcJobApiClient
from uipath.runtime.jobapi.contract import (
    IIpcLogSink,
    JobLogDto,
    LogLevel,
)
from uipath.runtime.jobapi.log_handler import (
    IpcSendLogHandler,
    PooledIpcSendLogHandler,
)
from uipath.runtime.jobapi.pooled import (
    PooledLogSink,
    get_pooled_log_sink,
    set_pooled_log_sink,
)

__all__ = [
    "IIpcLogSink",
    "IpcJobApiClient",
    "IpcSendLogHandler",
    "JobLogDto",
    "LogLevel",
    "PooledIpcSendLogHandler",
    "PooledLogSink",
    "get_pooled_log_sink",
    "set_pooled_log_sink",
]
