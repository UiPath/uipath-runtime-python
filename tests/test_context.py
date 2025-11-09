import json
from pathlib import Path
from typing import Any

import pytest

from uipath.runtime.context import UiPathRuntimeContext
from uipath.runtime.errors import (
    UiPathErrorCode,
    UiPathRuntimeError,
)
from uipath.runtime.result import UiPathRuntimeResult, UiPathRuntimeStatus


class DummyLogsInterceptor:
    """Minimal interceptor used to avoid touching real logging in tests."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.setup_called = False
        self.teardown_called = False

    def setup(self) -> None:
        self.setup_called = True

    def teardown(self) -> None:
        self.teardown_called = True


@pytest.fixture(autouse=True)
def patch_logs_interceptor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch UiPathRuntimeLogsInterceptor with a dummy so tests don't depend on logging."""
    monkeypatch.setattr(
        "uipath.runtime.context.UiPathRuntimeLogsInterceptor",
        DummyLogsInterceptor,
    )


def test_context_loads_json_input_file(tmp_path: Path) -> None:
    input_data = {"foo": "bar", "answer": 42}
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(input_data))

    ctx = UiPathRuntimeContext(input_file=str(input_path))

    with ctx:
        # input should be loaded from the JSON file
        assert ctx.input == input_data
        # logs interceptor should have been set up
        assert isinstance(ctx.logs_interceptor, DummyLogsInterceptor)
        assert ctx.logs_interceptor.setup_called

    # After leaving the context, interceptor should be torn down
    assert ctx.logs_interceptor.teardown_called


def test_context_raises_for_invalid_json(tmp_path: Path) -> None:
    bad_input_path = tmp_path / "input.json"
    bad_input_path.write_text("{not: valid json")  # invalid JSON

    ctx = UiPathRuntimeContext(input_file=str(bad_input_path))

    with pytest.raises(UiPathRuntimeError) as excinfo:
        with ctx:
            # __enter__ should fail before body executes
            pass

    err = excinfo.value.error_info
    assert err.code == f"Python.{UiPathErrorCode.INPUT_INVALID_JSON.value}"


def test_output_file_written_on_successful_execution(tmp_path: Path) -> None:
    output_path = tmp_path / "output.json"

    ctx = UiPathRuntimeContext(
        output_file=str(output_path),
    )

    with ctx:
        # Simulate a successful runtime that produced some output
        ctx.result = UiPathRuntimeResult(
            status=UiPathRuntimeStatus.SUCCESSFUL,
            output={"foo": "bar"},
        )
        pass

    assert output_path.exists()
    written = json.loads(output_path.read_text())
    assert written == {"foo": "bar"}


def test_result_file_written_on_success_contains_output(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime"
    ctx = UiPathRuntimeContext(
        job_id="job-123",  # triggers writing result file
        runtime_dir=str(runtime_dir),
        result_file="result.json",
    )

    with ctx:
        ctx.result = UiPathRuntimeResult(
            status=UiPathRuntimeStatus.SUCCESSFUL,
            output={"foo": "bar"},
        )
        pass

    # Assert: result file is written whether successful or faulted
    result_path = Path(ctx.result_file_path)
    assert result_path.exists()

    content = json.loads(result_path.read_text())

    # Should contain output and no error
    assert content["output"] == {"foo": "bar"}
    assert "error" not in content or content["error"] is None


def test_result_file_written_on_fault_contains_error_contract(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime"
    ctx = UiPathRuntimeContext(
        job_id="job-456",  # triggers writing result file
        runtime_dir=str(runtime_dir),
        result_file="result.json",
    )

    # No pre-set result -> context will create a default UiPathRuntimeResult()

    # Act: simulate a failing runtime
    with pytest.raises(RuntimeError, match="Stream blew up"):
        with ctx:
            raise RuntimeError("Stream blew up")

    # Assert: result file is written even when faulted
    result_path = Path(ctx.result_file_path)
    assert result_path.exists()

    content = json.loads(result_path.read_text())

    # We always have an output key, even if it's an empty dict
    assert "output" in content
    # Status should be FAULTED
    assert "status" in content
    assert content["status"] == UiPathRuntimeStatus.FAULTED.value
    # Error contract should be present and structured
    assert "error" in content
    error = content["error"]
    assert error["code"] == "ERROR_RuntimeError"
    assert error["title"] == "Runtime error: RuntimeError"
    assert "Stream blew up" in error["detail"]
