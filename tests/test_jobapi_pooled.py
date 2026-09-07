import logging
from typing import Any

import pytest

from uipath.runtime.jobapi.contract import JobLogDto, LogLevel
from uipath.runtime.jobapi.log_handler import PooledIpcSendLogHandler
from uipath.runtime.jobapi.pooled import get_pooled_log_sink, set_pooled_log_sink


@pytest.fixture(autouse=True)
def _clear_sink():
    # The sink is process-global; make sure a test never leaks it to the next.
    set_pooled_log_sink(None)
    yield
    set_pooled_log_sink(None)


def test_sink_registry_set_and_clear():
    assert get_pooled_log_sink() is None

    def sink(job_id: str, log: JobLogDto) -> None:
        pass

    set_pooled_log_sink(sink)
    assert get_pooled_log_sink() is sink

    set_pooled_log_sink(None)
    assert get_pooled_log_sink() is None


def test_pooled_handler_forwards_tagged_with_job_id():
    calls: list[tuple[str, Any]] = []
    handler = PooledIpcSendLogHandler(
        "job-key-1", lambda jid, log: calls.append((jid, log))
    )
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

    assert len(calls) == 1
    job_id, log = calls[0]
    assert job_id == "job-key-1"
    assert log.Message == "hello world"
    assert log.LogLevel == LogLevel.WARNING


def test_pooled_handler_swallows_sink_errors():
    def boom(job_id: str, log: JobLogDto) -> None:
        raise RuntimeError("pipe down")

    handler = PooledIpcSendLogHandler("job-key-1", boom)
    handler.setFormatter(logging.Formatter("%(message)s"))
    record = logging.LogRecord("n", logging.INFO, "p", 1, "x", None, None)

    # handleError writes to stderr but must not raise.
    handler.emit(record)
