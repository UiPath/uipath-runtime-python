"""The in-memory output sinks and how the context routes to them.

By default the runtime writes logs to execution.log and the result to output.json. When a host
installs a log handler / result sink, the runtime routes to them instead: logs to the handler, the
result to the sink. Suspended keeps output.json.
"""

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from uipath.runtime.context import UiPathRuntimeContext
from uipath.runtime.output_sinks import (
    get_log_handler,
    get_result_sink,
    set_log_handler,
    set_result_sink,
)
from uipath.runtime.result import UiPathRuntimeResult, UiPathRuntimeStatus


@pytest.fixture(autouse=True)
def _clear_sinks():
    set_log_handler(None)
    set_result_sink(None)
    yield
    set_log_handler(None)
    set_result_sink(None)


class _DummyInterceptor:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.log_handler = kwargs.get("log_handler")

    def setup(self) -> None:
        pass

    def teardown(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _patch_interceptor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "uipath.runtime.context.UiPathRuntimeLogsInterceptor", _DummyInterceptor
    )


def _ctx(tmp_path: Path) -> UiPathRuntimeContext:
    return UiPathRuntimeContext(
        job_id="job-key", runtime_dir=str(tmp_path / "rt"), result_file="output.json"
    )


def test_registry_set_and_clear() -> None:
    assert get_log_handler() is None
    assert get_result_sink() is None
    handler = logging.NullHandler()
    set_log_handler(handler)
    assert get_log_handler() is handler
    set_log_handler(None)
    assert get_log_handler() is None


def test_installed_log_handler_is_used(tmp_path: Path) -> None:
    handler = logging.NullHandler()
    set_log_handler(handler)
    ctx = _ctx(tmp_path)
    with ctx:
        pass
    assert ctx.logs_interceptor.log_handler is handler


def test_no_log_handler_defaults_to_file(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with ctx:
        pass
    assert ctx.logs_interceptor.log_handler is None


def test_result_sink_receives_result_and_args_file(tmp_path: Path) -> None:
    got: list[tuple[Any, str]] = []

    def _sink(result: Any, path: str) -> None:
        got.append((result, path))

    set_result_sink(_sink)
    ctx = _ctx(tmp_path)
    with ctx:
        ctx.result = UiPathRuntimeResult(
            status=UiPathRuntimeStatus.SUCCESSFUL, output={"foo": "bar"}
        )

    assert len(got) == 1
    result, path = got[0]
    assert result.status == UiPathRuntimeStatus.SUCCESSFUL
    # The output arguments were spilled to the file the sink was handed.
    assert json.loads(Path(path).read_text()) == {"foo": "bar"}
    # output.json is still written as a fallback.
    content = json.loads(Path(ctx.resolved_result_file_path).read_text())
    assert content["output"] == {"foo": "bar"}


def test_result_sink_not_called_for_suspended(tmp_path: Path) -> None:
    calls: list[int] = []

    def _sink(result: Any, path: str) -> None:
        calls.append(1)

    set_result_sink(_sink)
    ctx = _ctx(tmp_path)
    with ctx:
        ctx.result = UiPathRuntimeResult(status=UiPathRuntimeStatus.SUSPENDED)

    assert calls == []


def test_no_result_sink_leaves_result_on_file(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with ctx:
        ctx.result = UiPathRuntimeResult(
            status=UiPathRuntimeStatus.SUCCESSFUL, output={"a": 1}
        )

    content = json.loads(Path(ctx.resolved_result_file_path).read_text())
    assert content["output"] == {"a": 1}


def test_result_sink_exception_does_not_clobber_output_json(tmp_path: Path) -> None:
    """A sink failure is a side-channel failure: it must not fault the job or rewrite output.json.

    The sink (e.g. IPC delivery) runs after the authoritative output.json is written. A raise here
    must be swallowed, NOT caught by __exit__'s catch-all — which would overwrite the good result with
    a FAULTED shutdown error.
    """

    def _boom(result: Any, path: str) -> None:
        raise RuntimeError("ipc delivery failed")

    set_result_sink(_boom)
    ctx = _ctx(tmp_path)
    # No exception escapes the context even though the sink raises.
    with ctx:
        ctx.result = UiPathRuntimeResult(
            status=UiPathRuntimeStatus.SUCCESSFUL, output={"ok": 1}
        )

    content = json.loads(Path(ctx.resolved_result_file_path).read_text())
    # output.json still holds the SUCCESSFUL result — not clobbered as FAULTED (which drops output
    # and adds a RUNTIME_SHUTDOWN_ERROR).
    assert content["output"] == {"ok": 1}
    assert "error" not in content


def test_split_output_arguments_with_sink_does_not_double_write(tmp_path: Path) -> None:
    """With split_output_arguments AND a sink, the args file is written once and reused, not twice."""
    got: list[str] = []

    def _sink(result: Any, path: str) -> None:
        got.append(path)

    set_result_sink(_sink)
    ctx = UiPathRuntimeContext(
        job_id="job-key",
        runtime_dir=str(tmp_path / "rt"),
        result_file="output.json",
        split_output_arguments=True,
    )
    with ctx:
        ctx.result = UiPathRuntimeResult(
            status=UiPathRuntimeStatus.SUCCESSFUL, output={"foo": "bar"}
        )

    # The sink still gets the spilled args file, whose content is the output arguments.
    assert len(got) == 1
    assert json.loads(Path(got[0]).read_text()) == {"foo": "bar"}
    # output.json carries the pointer (split), not the inline output.
    content = json.loads(Path(ctx.resolved_result_file_path).read_text())
    assert content["outputArgumentsFilePath"] == got[0]
    assert "output" not in content


def test_sinks_are_isolated_per_context() -> None:
    """Concurrency-safety: sinks live per execution context, so concurrent jobs don't cross-wire.

    Installed in one context (contextvars) they are invisible in another and in the caller — which is
    what lets several jobs run in one process without clobbering each other's routing.
    """
    import contextvars

    handler = logging.NullHandler()

    def _sink(result: Any, path: str) -> None:
        return None

    def _install() -> None:
        set_log_handler(handler)
        set_result_sink(_sink)

    other = contextvars.copy_context()
    other.run(_install)

    # Visible inside the context that installed them...
    assert other.run(get_log_handler) is handler
    assert other.run(get_result_sink) is _sink
    # ...but not in the caller's context.
    assert get_log_handler() is None
    assert get_result_sink() is None


def test_result_sink_is_snapshotted_at_enter(tmp_path: Path) -> None:
    """The sink captured at __enter__ is the one used at __exit__.

    A registry swap after the context starts (another execution installing its own sink) must not
    reroute this context's result — it delivers to the sink present when it began.
    """
    delivered: list[str] = []

    def _first(result: Any, path: str) -> None:
        delivered.append("first")

    def _second(result: Any, path: str) -> None:
        delivered.append("second")

    set_result_sink(_first)
    ctx = _ctx(tmp_path)
    with ctx:
        # Something else replaces the process-global sink mid-run.
        set_result_sink(_second)
        ctx.result = UiPathRuntimeResult(
            status=UiPathRuntimeStatus.SUCCESSFUL, output={"k": "v"}
        )

    assert delivered == ["first"]
