"""A run whose task is cancelled ends as stopped rather than faulted."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from uipath.runtime.context import UiPathRuntimeContext
from uipath.runtime.output_sinks import set_result_sink
from uipath.runtime.result import UiPathRuntimeResult, UiPathRuntimeStatus


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
    set_result_sink(None)
    yield
    set_result_sink(None)


def _ctx(tmp_path: Path) -> UiPathRuntimeContext:
    return UiPathRuntimeContext(
        job_id="job-key", runtime_dir=str(tmp_path / "rt"), result_file="output.json"
    )


def _written(ctx: UiPathRuntimeContext) -> dict[str, Any]:
    return json.loads(Path(ctx.resolved_result_file_path).read_text())


async def _cancel_while_running(body: Any) -> None:
    task = asyncio.create_task(body())
    await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.wait({task})


@pytest.mark.asyncio
async def test_a_cancelled_run_ends_as_stopped(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)

    async def body() -> None:
        with ctx:
            await asyncio.sleep(10)

    await _cancel_while_running(body)

    content = _written(ctx)
    assert content["status"] == UiPathRuntimeStatus.STOPPED.value
    assert content.get("error") is None


@pytest.mark.asyncio
async def test_a_stray_cancelled_error_still_faults(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)

    with pytest.raises(asyncio.CancelledError):
        with ctx:
            child = asyncio.ensure_future(asyncio.sleep(10))
            await asyncio.sleep(0)
            child.cancel()
            await child

    content = _written(ctx)
    assert content["status"] == UiPathRuntimeStatus.FAULTED.value
    assert content["error"]["code"] == "ERROR_CancelledError"


def test_a_cancelled_error_outside_an_event_loop_faults(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)

    with pytest.raises(asyncio.CancelledError):
        with ctx:
            raise asyncio.CancelledError()

    assert _written(ctx)["status"] == UiPathRuntimeStatus.FAULTED.value


@pytest.mark.asyncio
async def test_an_error_while_stopping_ends_as_stopped(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)

    async def body() -> None:
        with ctx:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                raise RuntimeError("cleanup failed while stopping") from None

    await _cancel_while_running(body)

    assert _written(ctx)["status"] == UiPathRuntimeStatus.STOPPED.value


@pytest.mark.asyncio
async def test_an_error_in_a_run_nobody_cancelled_faults(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)

    with pytest.raises(ValueError):
        with ctx:
            raise ValueError("bad input")

    content = _written(ctx)
    assert content["status"] == UiPathRuntimeStatus.FAULTED.value
    assert content["error"]["code"] == "ERROR_ValueError"


@pytest.mark.asyncio
async def test_the_result_sink_receives_a_stopped_result(tmp_path: Path) -> None:
    delivered: list[UiPathRuntimeResult] = []
    set_result_sink(lambda result, _path: delivered.append(result))
    ctx = _ctx(tmp_path)

    async def body() -> None:
        with ctx:
            await asyncio.sleep(10)

    await _cancel_while_running(body)

    assert [r.status for r in delivered] == [UiPathRuntimeStatus.STOPPED]
