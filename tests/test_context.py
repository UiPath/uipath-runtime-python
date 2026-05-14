import json
from pathlib import Path
from typing import Any

import pytest
from uipath.core.errors import ErrorCategory, UiPathFaultedTriggerError

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
        assert ctx.get_input() == input_data
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
            # Explicitly call get_input() which will raise
            ctx.get_input()

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
    result_path = Path(ctx.resolved_result_file_path)
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
    result_path = Path(ctx.resolved_result_file_path)
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


def test_parse_input_string_returns_none_for_empty_string() -> None:
    """Test that empty input string returns None, not empty dict."""
    ctx = UiPathRuntimeContext(input="")

    result = ctx.get_input()

    assert result is None


def test_with_defaults_overlays_ids_from_input(tmp_path: Path) -> None:
    ctx = UiPathRuntimeContext.with_defaults(
        config_path=str(tmp_path / "missing.json"),
        input=json.dumps(
            {
                "uipath__conversation_id": "conv-from-input",
                "uipath__exchange_id": "exch-from-input",
            }
        ),
    )

    assert ctx.conversation_id == "conv-from-input"
    assert ctx.exchange_id == "exch-from-input"


def test_with_defaults_input_wins_over_fps(tmp_path: Path) -> None:
    config_path = tmp_path / "uipath.json"
    config_path.write_text(
        json.dumps(
            {
                "fpsProperties": {
                    "conversationalService.conversationId": "conv-from-fps",
                    "conversationalService.exchangeId": "exch-from-fps",
                }
            }
        )
    )

    ctx = UiPathRuntimeContext.with_defaults(
        config_path=str(config_path),
        input=json.dumps(
            {
                "uipath__conversation_id": "conv-from-input",
                "uipath__exchange_id": "exch-from-input",
            }
        ),
    )

    assert ctx.conversation_id == "conv-from-input"
    assert ctx.exchange_id == "exch-from-input"


def test_with_defaults_preserves_fps_when_input_missing_keys(tmp_path: Path) -> None:
    config_path = tmp_path / "uipath.json"
    config_path.write_text(
        json.dumps(
            {
                "fpsProperties": {
                    "conversationalService.conversationId": "conv-from-fps",
                    "conversationalService.exchangeId": "exch-from-fps",
                }
            }
        )
    )

    ctx = UiPathRuntimeContext.with_defaults(
        config_path=str(config_path),
        input=json.dumps({"messages": []}),
    )

    assert ctx.conversation_id == "conv-from-fps"
    assert ctx.exchange_id == "exch-from-fps"


def test_with_defaults_noop_when_no_input(tmp_path: Path) -> None:
    config_path = tmp_path / "uipath.json"
    config_path.write_text(
        json.dumps(
            {
                "fpsProperties": {
                    "conversationalService.conversationId": "conv-from-fps",
                }
            }
        )
    )

    ctx = UiPathRuntimeContext.with_defaults(config_path=str(config_path))

    assert ctx.conversation_id == "conv-from-fps"


def test_with_defaults_swallows_invalid_json_input(tmp_path: Path) -> None:
    """Invalid input must not crash construction — the real get_input() call
    during execute surfaces the JSON error at the right layer."""
    config_path = tmp_path / "uipath.json"
    config_path.write_text(
        json.dumps(
            {
                "fpsProperties": {
                    "conversationalService.conversationId": "conv-from-fps",
                }
            }
        )
    )

    ctx = UiPathRuntimeContext.with_defaults(
        config_path=str(config_path),
        input="{not: valid json",
    )

    assert ctx.conversation_id == "conv-from-fps"


def test_parse_input_string_returns_none_for_whitespace_only() -> None:
    """Test that whitespace-only input string returns None, not empty dict."""
    ctx = UiPathRuntimeContext(input="   ")

    result = ctx.get_input()

    assert result is None


def test_parse_input_string_returns_none_for_none() -> None:
    """Test that None input returns None."""
    ctx = UiPathRuntimeContext(input=None)

    result = ctx.get_input()

    assert result is None


def test_from_config_extracts_fps_properties_without_runtime(tmp_path: Path) -> None:
    """fpsProperties should be loaded even if 'runtime' block is missing."""
    cfg = {
        "fpsProperties": {
            "conversationalService.conversationId": "conv-123",
            "conversationalService.exchangeId": "ex-456",
            "conversationalService.messageId": "msg-789",
            "mcpServer.id": "server-id-123",
            "mcpServer.slug": "my-mcp-server",
        }
    }
    config_path = tmp_path / "uipath.json"
    config_path.write_text(json.dumps(cfg))

    ctx = UiPathRuntimeContext.from_config(config_path=str(config_path))

    assert ctx.conversation_id == "conv-123"
    assert ctx.exchange_id == "ex-456"
    assert ctx.message_id == "msg-789"
    assert ctx.mcp_server_id == "server-id-123"
    assert ctx.mcp_server_slug == "my-mcp-server"


def test_from_config_loads_runtime_and_fps_properties(tmp_path: Path) -> None:
    """runtime.* keys and fpsProperties.* keys should both be applied."""
    cfg = {
        "runtime": {
            "dir": "my_runtime",
            "outputFile": "my_output.json",
            "stateFile": "my_state.db",
            "logsFile": "my_logs.log",
            "internalArguments": {"parentOperationId": "operationId-123"},
        },
        "fpsProperties": {
            "conversationalService.conversationId": "conv-abc",
            "conversationalService.exchangeId": "ex-def",
            "conversationalService.messageId": "msg-ghi",
            "mcpServer.id": "mcp-server-456",
            "mcpServer.slug": "test-server-slug",
        },
    }
    config_path = tmp_path / "uipath.json"
    config_path.write_text(json.dumps(cfg))

    ctx = UiPathRuntimeContext.from_config(config_path=str(config_path))

    # runtime mapping
    assert ctx.runtime_dir == "my_runtime"
    assert (
        ctx.result_file == "my_output.json"
    )  # outputFile maps to result_file (serverless contract)
    assert ctx.state_file == "my_state.db"
    assert ctx.logs_file == "my_logs.log"

    # parentOperationId is mapped correctly from internal_arguments
    assert ctx.parent_operation_id == "operationId-123"

    # fpsProperties mapping
    assert ctx.conversation_id == "conv-abc"
    assert ctx.exchange_id == "ex-def"
    assert ctx.message_id == "msg-ghi"
    assert ctx.mcp_server_id == "mcp-server-456"
    assert ctx.mcp_server_slug == "test-server-slug"


def test_result_file_written_on_faulted_trigger_error(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime"
    ctx = UiPathRuntimeContext(
        job_id="job-trigger-test",
        runtime_dir=str(runtime_dir),
        result_file="result.json",
    )

    trigger_error = UiPathFaultedTriggerError(
        ErrorCategory.SYSTEM, "Failed to create HITL action", "validation error"
    )
    trigger_error.category = ErrorCategory.SYSTEM
    trigger_error.message = "Failed to create HITL action"

    with pytest.raises(UiPathFaultedTriggerError):
        with ctx:
            raise trigger_error

    result_path = Path(ctx.resolved_result_file_path)
    assert result_path.exists()

    content = json.loads(result_path.read_text())
    assert content["status"] == UiPathRuntimeStatus.FAULTED.value
    assert "error" in content

    error = content["error"]
    assert error["code"] == f"Python.{UiPathErrorCode.RESUME_TRIGGER_ERROR.value}"
    assert error["title"] == "Resume trigger error"
    assert "Failed to create HITL action" in error["detail"]
    assert error["category"] == ErrorCategory.SYSTEM.value


def test_string_output_wrapped_in_dict() -> None:
    """Test that string output is wrapped in a dict with key 'output'."""
    result = UiPathRuntimeResult(
        status=UiPathRuntimeStatus.SUCCESSFUL,
        output="primitive str",
    )

    result_dict = result.to_dict()

    assert result_dict["output"] == {"output": "primitive str"}
    assert result_dict["status"] == UiPathRuntimeStatus.SUCCESSFUL
