"""Logging handlers that forward records to the handler over uipath-ipc."""

import logging

from uipath.runtime.jobapi.client import IpcJobApiClient
from uipath.runtime.jobapi.contract import JobLogDto, LogLevel
from uipath.runtime.jobapi.pooled import PooledLogSink


def _to_log_level(levelno: int) -> int:
    if levelno >= logging.CRITICAL:
        return LogLevel.CRITICAL
    if levelno >= logging.ERROR:
        return LogLevel.ERROR
    if levelno >= logging.WARNING:
        return LogLevel.WARNING
    if levelno >= logging.INFO:
        return LogLevel.INFORMATION
    if levelno >= logging.DEBUG:
        return LogLevel.DEBUG
    return LogLevel.TRACE


class IpcSendLogHandler(logging.Handler):
    """Forwards each record to the handler's IIpcLogSink via the non-pooled per-job client."""

    def __init__(self, client: IpcJobApiClient) -> None:
        """Wrap the IPC client the records are forwarded through."""
        super().__init__()
        self._client = client

    def emit(self, record: logging.LogRecord) -> None:
        """Format the record and hand it to the IPC client (non-blocking)."""
        try:
            message = self.format(record)
            self._client.send_log(
                JobLogDto(Message=message, LogLevel=_to_log_level(record.levelno))
            )
        except Exception:
            self.handleError(record)


class PooledIpcSendLogHandler(logging.Handler):
    """Forwards each record to the pooled process-global sink, tagged with the job id."""

    def __init__(self, job_id: str, sink: PooledLogSink) -> None:
        """Bind the job id every record is tagged with and the sink to forward through."""
        super().__init__()
        self._job_id = job_id
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        """Format the record and hand it to the pooled sink (non-blocking)."""
        try:
            message = self.format(record)
            self._sink(
                self._job_id,
                JobLogDto(Message=message, LogLevel=_to_log_level(record.levelno)),
            )
        except Exception:
            self.handleError(record)
