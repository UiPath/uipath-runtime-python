import logging

from uipath.runtime.jobapi.contract import JobLogDto, LogLevel
from uipath.runtime.jobapi.log_handler import IpcSendLogHandler, _to_log_level


def test_to_log_level_mapping():
    assert _to_log_level(logging.CRITICAL) == LogLevel.CRITICAL
    assert _to_log_level(logging.ERROR) == LogLevel.ERROR
    assert _to_log_level(logging.WARNING) == LogLevel.WARNING
    assert _to_log_level(logging.INFO) == LogLevel.INFORMATION
    assert _to_log_level(logging.DEBUG) == LogLevel.DEBUG
    # Below DEBUG collapses to TRACE; above CRITICAL clamps to CRITICAL.
    assert _to_log_level(1) == LogLevel.TRACE
    assert _to_log_level(logging.CRITICAL + 10) == LogLevel.CRITICAL


class _RecordingClient:
    def __init__(self):
        self.logs: list[JobLogDto] = []

    def send_log(self, dto: JobLogDto) -> None:
        self.logs.append(dto)


def test_emit_forwards_formatted_message_and_level():
    client = _RecordingClient()
    handler = IpcSendLogHandler(client)  # type: ignore[arg-type]
    handler.setFormatter(logging.Formatter("%(message)s"))

    record = logging.LogRecord(
        name="n",
        level=logging.WARNING,
        pathname="p",
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    handler.emit(record)

    assert len(client.logs) == 1
    assert client.logs[0].Message == "hello world"
    assert client.logs[0].LogLevel == LogLevel.WARNING


def test_emit_swallows_client_errors():
    class _Boom:
        def send_log(self, dto: JobLogDto) -> None:
            raise RuntimeError("pipe down")

    handler = IpcSendLogHandler(_Boom())  # type: ignore[arg-type]
    handler.setFormatter(logging.Formatter("%(message)s"))
    record = logging.LogRecord("n", logging.INFO, "p", 1, "x", None, None)

    # handleError writes to sys.stderr but must not raise.
    handler.emit(record)
