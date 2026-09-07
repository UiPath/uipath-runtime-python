"""Python mirror of the .NET ``IIpcLogSink`` CoreIpc contract (logs only).

uipath-ipc routes by the contract class ``__name__`` and method name and serializes each
argument by its declared field names, so the class name, the method name, and every field
below must match the .NET side exactly. The result is not sent over IPC — it stays on
``output.json``, which the handler still reads.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import IntEnum


class LogLevel(IntEnum):
    """Mirror of Microsoft.Extensions.Logging.LogLevel (the wire values)."""

    TRACE = 0
    DEBUG = 1
    INFORMATION = 2
    WARNING = 3
    ERROR = 4
    CRITICAL = 5
    NONE = 6


@dataclass
class JobLogDto:
    """A single log entry (PascalCase fields to match the wire)."""

    Message: str = ""
    LogLevel: int = LogLevel.INFORMATION.value


class IIpcLogSink(ABC):
    """The handler's log-sink contract; the class name is the CoreIpc endpoint key."""

    @abstractmethod
    async def SendLog(self, jobId: str, log: JobLogDto) -> None:
        """Forward one log entry (one-way)."""
