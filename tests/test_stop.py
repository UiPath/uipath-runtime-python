"""A run its host stops ends as stopped rather than faulted."""

import asyncio
import json
from pathlib import Path
from typing import Any, Callable, Coroutine

import pytest

from uipath.runtime.context import UiPathRuntimeContext
from uipath.runtime.output_sinks import set_result_sink
from uipath.runtime.result import UiPathRuntimeResult, UiPathRuntimeStatus
from uipath.runtime.stop import set_stop_requested_probe


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


class _Host:
    """Stops a run the way uipath server does: it records the stop, then cancels the task."""

    def __init__(self) -> None:
        self.stopped = False

    def install_probe(self) -> None:
        set_stop_requested_probe(lambda: self.stopped)

    async def stop_while_running(
        self, body: Callable[[], Coroutine[Any, Any, None]]
    ) -> None:
        task = asyncio.create_task(body())
        await asyncio.sleep(0.01)
        self.stopped = True
        task.cancel()
        await asyncio.wait({task})


def _ctx(tmp_path: Path) -> UiPathRuntimeContext:
    return UiPathRuntimeContext(
        job_id="job-key", runtime_dir=str(tmp_path / "rt"), result_file="output.json"
    )


def _written(ctx: UiPathRuntimeContext) -> dict[str, Any]:
    return json.loads(Path(ctx.resolved_result_file_path).read_text())


@pytest.mark.asyncio
async def test_a_run_the_host_stops_ends_as_stopped(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    host = _Host()

    async def body() -> None:
        host.install_probe()
        with ctx:
            await asyncio.sleep(10)

    await host.stop_while_running(body)

    content = _written(ctx)
    assert content["status"] == UiPathRuntimeStatus.STOPPED.value
    assert content.get("error") is None


@pytest.mark.asyncio
async def test_a_cancelled_run_without_a_probe_faults(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)

    async def body() -> None:
        with ctx:
            await asyncio.sleep(10)

    await _Host().stop_while_running(body)

    content = _written(ctx)
    assert content["status"] == UiPathRuntimeStatus.FAULTED.value
    assert content["error"]["code"] == "ERROR_CancelledError"


@pytest.mark.asyncio
async def test_a_cancellation_the_host_did_not_ask_for_faults(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    host = _Host()

    async def body() -> None:
        host.install_probe()
        with ctx:
            child = asyncio.ensure_future(asyncio.sleep(10))
            await asyncio.sleep(0)
            child.cancel()
            await child

    task = asyncio.create_task(body())
    with pytest.raises(asyncio.CancelledError):
        await task

    content = _written(ctx)
    assert content["status"] == UiPathRuntimeStatus.FAULTED.value
    assert content["error"]["code"] == "ERROR_CancelledError"


@pytest.mark.asyncio
async def test_a_failing_probe_faults(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    host = _Host()

    def broken_probe() -> bool:
        raise RuntimeError("probe failed")

    async def body() -> None:
        set_stop_requested_probe(broken_probe)
        with ctx:
            await asyncio.sleep(10)

    await host.stop_while_running(body)

    assert _written(ctx)["status"] == UiPathRuntimeStatus.FAULTED.value


@pytest.mark.asyncio
async def test_an_error_while_stopping_ends_as_stopped(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    host = _Host()

    async def body() -> None:
        host.install_probe()
        with ctx:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                raise RuntimeError("cleanup failed while stopping") from None

    await host.stop_while_running(body)

    assert _written(ctx)["status"] == UiPathRuntimeStatus.STOPPED.value


@pytest.mark.asyncio
async def test_an_error_of_its_own_faults_even_once_a_stop_is_requested(
    tmp_path: Path,
) -> None:
    ctx = _ctx(tmp_path)
    host = _Host()
    host.stopped = True

    async def body() -> None:
        host.install_probe()
        with ctx:
            raise ValueError("bad input")

    task = asyncio.create_task(body())
    with pytest.raises(ValueError):
        await task

    content = _written(ctx)
    assert content["status"] == UiPathRuntimeStatus.FAULTED.value
    assert content["error"]["code"] == "ERROR_ValueError"


@pytest.mark.asyncio
async def test_a_child_timeout_while_a_task_group_exits_faults(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    host = _Host()

    async def time_out_soon() -> None:
        async with asyncio.timeout(0.01):
            await asyncio.sleep(10)

    async def body() -> None:
        host.install_probe()
        with ctx:
            async with asyncio.TaskGroup() as group:
                group.create_task(time_out_soon())

    task = asyncio.create_task(body())
    with pytest.raises(ExceptionGroup):
        await task

    content = _written(ctx)
    assert content["status"] == UiPathRuntimeStatus.FAULTED.value
    assert content["error"]["code"] == "ERROR_ExceptionGroup"


@pytest.mark.asyncio
async def test_a_task_group_stopped_while_its_child_cleans_up_ends_as_stopped(
    tmp_path: Path,
) -> None:
    ctx = _ctx(tmp_path)
    host = _Host()

    async def fail_on_cancel() -> None:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            raise RuntimeError("cleanup failed while stopping") from None

    async def body() -> None:
        host.install_probe()
        with ctx:
            async with asyncio.TaskGroup() as group:
                group.create_task(fail_on_cancel())
                await asyncio.sleep(10)

    await host.stop_while_running(body)

    assert _written(ctx)["status"] == UiPathRuntimeStatus.STOPPED.value


@pytest.mark.asyncio
async def test_the_result_sink_receives_a_stopped_result(tmp_path: Path) -> None:
    delivered: list[UiPathRuntimeResult] = []
    set_result_sink(lambda result, _path: delivered.append(result))
    ctx = _ctx(tmp_path)
    host = _Host()

    async def body() -> None:
        host.install_probe()
        with ctx:
            await asyncio.sleep(10)

    await host.stop_while_running(body)

    assert [r.status for r in delivered] == [UiPathRuntimeStatus.STOPPED]
