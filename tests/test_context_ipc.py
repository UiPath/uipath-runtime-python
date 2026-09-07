"""Context wiring for the job-API IPC channel (logs only).

The real IpcJobApiClient (threads + named pipes) is replaced by a recording fake
so these tests exercise the context's branching, not the transport — the
transport itself is covered by test_jobapi_client.py. The result stays on
output.json; only the logs move to IPC (and execution.log is suppressed).
"""

import json
from pathlib import Path
from typing import Any

import pytest

from uipath.runtime.context import UiPathRuntimeContext
from uipath.runtime.jobapi.log_handler import IpcSendLogHandler, PooledIpcSendLogHandler
from uipath.runtime.jobapi.pooled import set_pooled_log_sink
from uipath.runtime.result import UiPathRuntimeResult, UiPathRuntimeStatus


class _FakeIpcClient:
    def __init__(self, endpoint: str, job_id: str, log: Any) -> None:
        self.endpoint = endpoint
        self.job_id = job_id
        self.started = False
        self.closed = False
        self.logs: list[Any] = []

    def start(self) -> None:
        self.started = True

    def send_log(self, dto: Any) -> None:
        self.logs.append(dto)

    def close(self) -> None:
        self.closed = True


class _DummyInterceptor:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.log_handler = kwargs.get("log_handler")

    def setup(self) -> None:
        pass

    def teardown(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _patch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("uipath.runtime.context.IpcJobApiClient", _FakeIpcClient)
    monkeypatch.setattr(
        "uipath.runtime.context.UiPathRuntimeLogsInterceptor", _DummyInterceptor
    )


def _ipc_ctx(tmp_path: Path, **kwargs: Any) -> UiPathRuntimeContext:
    return UiPathRuntimeContext(
        job_id="job-key",
        runtime_dir=str(tmp_path / "rt"),
        result_file="output.json",
        ipc_endpoint="the-pipe",
        ipc_job_id="job-guid",
        **kwargs,
    )


def test_with_defaults_reads_ipc_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("UIPATH_JOB_API_IPC_ENDPOINT", "ep")
    monkeypatch.setenv("UIPATH_JOB_ID", "jid")

    ctx = UiPathRuntimeContext.with_defaults()

    assert ctx.ipc_endpoint == "ep"
    assert ctx.ipc_job_id == "jid"
    assert ctx.ipc_active is True


def test_ipc_inactive_without_both_env_vars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("UIPATH_JOB_API_IPC_ENDPOINT", "ep")
    monkeypatch.delenv("UIPATH_JOB_ID", raising=False)

    ctx = UiPathRuntimeContext.with_defaults()

    assert ctx.ipc_active is False


def test_enter_starts_client_and_injects_ipc_log_handler(tmp_path: Path) -> None:
    ctx = _ipc_ctx(tmp_path)
    with ctx:
        pass

    client = ctx.ipc_client
    assert client.started is True
    assert client.endpoint == "the-pipe"
    assert client.job_id == "job-guid"
    # The interceptor was handed our IPC handler (so no execution.log is opened).
    assert isinstance(ctx.logs_interceptor.log_handler, IpcSendLogHandler)


def test_result_still_written_to_file_when_ipc_active(tmp_path: Path) -> None:
    ctx = _ipc_ctx(tmp_path)
    with ctx:
        ctx.result = UiPathRuntimeResult(
            status=UiPathRuntimeStatus.SUCCESSFUL, output={"foo": "bar"}
        )

    # Only the logs move to IPC; the result stays on output.json for the handler.
    content = json.loads(Path(ctx.resolved_result_file_path).read_text())
    assert content["output"] == {"foo": "bar"}
    assert ctx.ipc_client.closed is True


def test_pooled_ipc_injects_pooled_handler_result_stays_on_file(tmp_path: Path) -> None:
    # Pooled: a job id but no endpoint, plus a registered process-global sink.
    calls: list[Any] = []
    set_pooled_log_sink(lambda job_id, log: calls.append((job_id, log)))
    try:
        ctx = UiPathRuntimeContext(
            job_id="job-key",
            runtime_dir=str(tmp_path / "rt"),
            result_file="output.json",
            ipc_job_id="job-guid",  # no ipc_endpoint -> pooled path
        )
        assert ctx.pooled_ipc_active is True

        with ctx:
            ctx.result = UiPathRuntimeResult(
                status=UiPathRuntimeStatus.SUCCESSFUL, output={"foo": "bar"}
            )

        # The interceptor got the pooled handler (so execution.log is suppressed), and no per-job
        # client was created (pooled forwards through the process-global sink instead).
        assert isinstance(ctx.logs_interceptor.log_handler, PooledIpcSendLogHandler)
        assert ctx.ipc_client is None
        # The result still lands on output.json (logs-only channel).
        content = json.loads(Path(ctx.resolved_result_file_path).read_text())
        assert content["output"] == {"foo": "bar"}
    finally:
        set_pooled_log_sink(None)


def test_no_ipc_log_handler_when_inactive(tmp_path: Path) -> None:
    ctx = UiPathRuntimeContext(
        job_id="job-key",
        runtime_dir=str(tmp_path / "rt"),
        result_file="output.json",
    )
    with ctx:
        ctx.result = UiPathRuntimeResult(
            status=UiPathRuntimeStatus.SUCCESSFUL, output={"foo": "bar"}
        )

    # No IPC → the interceptor builds its own (file) handler and no client is made.
    assert ctx.logs_interceptor.log_handler is None
    assert ctx.ipc_client is None
