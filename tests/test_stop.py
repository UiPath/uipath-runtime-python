"""A run the host asked to stop ends as stopped rather than faulted."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from uipath.runtime.context import UiPathRuntimeContext
from uipath.runtime.output_sinks import set_result_sink
from uipath.runtime.result import UiPathRuntimeResult, UiPathRuntimeStatus
from uipath.runtime.stop import get_stop_requested_probe, set_stop_requested_probe


class _DummyInterceptor:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def setup(self) -> None:
        pass

    def teardown(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(
        "uipath.runtime.context.UiPathRuntimeLogsInterceptor", _DummyInterceptor
    )
    set_stop_requested_probe(None)
    set_result_sink(None)
    yield
    set_stop_requested_probe(None)
    set_result_sink(None)


def _ctx(tmp_path: Path) -> UiPathRuntimeContext:
    return UiPathRuntimeContext(
        job_id="job-key", runtime_dir=str(tmp_path / "rt"), result_file="output.json"
    )


def _written(ctx: UiPathRuntimeContext) -> dict[str, Any]:
    return json.loads(Path(ctx.resolved_result_file_path).read_text())


def _run_cancelled(ctx: UiPathRuntimeContext) -> None:
    with pytest.raises(asyncio.CancelledError):
        with ctx:
            raise asyncio.CancelledError()


def test_probe_is_unset_by_default() -> None:
    assert get_stop_requested_probe() is None


def test_a_cancellation_the_host_asked_for_ends_as_stopped(tmp_path: Path) -> None:
    set_stop_requested_probe(lambda: True)
    ctx = _ctx(tmp_path)

    _run_cancelled(ctx)

    content = _written(ctx)
    assert content["status"] == UiPathRuntimeStatus.STOPPED.value
    assert content.get("error") is None


def test_a_cancellation_without_a_probe_still_faults(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)

    _run_cancelled(ctx)

    content = _written(ctx)
    assert content["status"] == UiPathRuntimeStatus.FAULTED.value
    assert content["error"]["code"] == "ERROR_CancelledError"


def test_a_cancellation_the_host_did_not_ask_for_faults(tmp_path: Path) -> None:
    set_stop_requested_probe(lambda: False)
    ctx = _ctx(tmp_path)

    _run_cancelled(ctx)

    assert _written(ctx)["status"] == UiPathRuntimeStatus.FAULTED.value


def test_an_error_while_stopping_ends_as_stopped(tmp_path: Path) -> None:
    set_stop_requested_probe(lambda: True)
    ctx = _ctx(tmp_path)

    with pytest.raises(RuntimeError):
        with ctx:
            try:
                raise asyncio.CancelledError()
            except asyncio.CancelledError:
                raise RuntimeError("cleanup failed while stopping") from None

    assert _written(ctx)["status"] == UiPathRuntimeStatus.STOPPED.value


def test_an_unrelated_error_still_faults_after_a_stop_request(tmp_path: Path) -> None:
    set_stop_requested_probe(lambda: True)
    ctx = _ctx(tmp_path)

    with pytest.raises(ValueError):
        with ctx:
            raise ValueError("bad input")

    content = _written(ctx)
    assert content["status"] == UiPathRuntimeStatus.FAULTED.value
    assert content["error"]["code"] == "ERROR_ValueError"


def test_a_run_that_completes_after_a_stop_request_keeps_its_result(
    tmp_path: Path,
) -> None:
    set_stop_requested_probe(lambda: True)
    ctx = _ctx(tmp_path)

    with ctx:
        ctx.result = UiPathRuntimeResult(output={"done": True})

    content = _written(ctx)
    assert content["status"] == UiPathRuntimeStatus.SUCCESSFUL.value
    assert content["output"] == {"done": True}


def test_a_probe_that_raises_reports_the_run_as_faulted(tmp_path: Path) -> None:
    def _broken() -> bool:
        raise RuntimeError("probe broke")

    set_stop_requested_probe(_broken)
    ctx = _ctx(tmp_path)

    _run_cancelled(ctx)

    assert _written(ctx)["status"] == UiPathRuntimeStatus.FAULTED.value


def test_the_result_sink_receives_a_stopped_result(tmp_path: Path) -> None:
    delivered: list[UiPathRuntimeResult] = []
    set_result_sink(lambda result, _path: delivered.append(result))
    set_stop_requested_probe(lambda: True)
    ctx = _ctx(tmp_path)

    _run_cancelled(ctx)

    assert [r.status for r in delivered] == [UiPathRuntimeStatus.STOPPED]


def test_the_probe_is_read_when_the_context_starts(tmp_path: Path) -> None:
    set_stop_requested_probe(lambda: True)
    ctx = _ctx(tmp_path)

    with pytest.raises(asyncio.CancelledError):
        with ctx:
            set_stop_requested_probe(None)
            raise asyncio.CancelledError()

    assert _written(ctx)["status"] == UiPathRuntimeStatus.STOPPED.value
