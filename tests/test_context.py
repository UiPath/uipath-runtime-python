import json
import os
from pathlib import Path
from typing import Any

import pytest
from uipath.core.errors import ErrorCategory, UiPathFaultedTriggerError
from uipath.core.triggers import UiPathResumeTrigger

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
            "conversationalService.enableOutputs": True,
            "conversationalService.conversationalUserId": "owner-guid",
            "conversationalService.runAsMe": True,
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
    assert ctx.conversational_outputs_enabled is True
    assert ctx.conversational_user_id == "owner-guid"
    assert ctx.conversational_run_as_me is True
    assert ctx.mcp_server_id == "server-id-123"
    assert ctx.mcp_server_slug == "my-mcp-server"


def test_conversational_run_as_me_defaults_false_when_fps_property_absent(
    tmp_path: Path,
) -> None:
    """conversational_run_as_me defaults to False when the fps key is missing."""
    cfg = {
        "fpsProperties": {
            "conversationalService.conversationId": "conv-123",
        }
    }
    config_path = tmp_path / "uipath.json"
    config_path.write_text(json.dumps(cfg))

    ctx = UiPathRuntimeContext.from_config(config_path=str(config_path))

    assert ctx.conversational_run_as_me is False


def test_from_config_conversational_outputs_enabled_defaults_false(
    tmp_path: Path,
) -> None:
    """When enableOutputs isn't in fpsProperties, the field defaults to False —
    legacy safe behavior for pre-migration conversational agents."""
    cfg = {
        "fpsProperties": {
            "conversationalService.conversationId": "conv-legacy",
        }
    }
    config_path = tmp_path / "uipath.json"
    config_path.write_text(json.dumps(cfg))

    ctx = UiPathRuntimeContext.from_config(config_path=str(config_path))

    assert ctx.conversational_outputs_enabled is False


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


def test_from_config_maps_end_exchange_fps_property(tmp_path: Path) -> None:
    """conversationalService.endExchange should map onto end_exchange."""
    cfg = {
        "fpsProperties": {
            "conversationalService.conversationId": "conv-123",
            "conversationalService.exchangeId": "ex-456",
            "conversationalService.endExchange": False,
        }
    }
    config_path = tmp_path / "uipath.json"
    config_path.write_text(json.dumps(cfg))

    ctx = UiPathRuntimeContext.from_config(config_path=str(config_path))

    assert ctx.end_exchange is False


def test_end_exchange_defaults_true_when_fps_property_absent(tmp_path: Path) -> None:
    """end_exchange defaults to True (legacy behavior) when the fps key is missing."""
    cfg = {
        "fpsProperties": {
            "conversationalService.conversationId": "conv-123",
        }
    }
    config_path = tmp_path / "uipath.json"
    config_path.write_text(json.dumps(cfg))

    ctx = UiPathRuntimeContext.from_config(config_path=str(config_path))

    assert ctx.end_exchange is True


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("false", False),
        ("False", False),
        ("FALSE", False),
        ("", False),
        ("true", True),
        ("True", True),
        ("TRUE", True),
    ],
)
def test_from_config_coerces_stringified_bool_fps_property(
    tmp_path: Path, raw: str, expected: bool
) -> None:
    """Stringified booleans must be parsed, not stored raw.

    Some producers deliver fpsProperties as a string->string map, so a boolean
    false arrives as "false". Stored raw on a bool field it stays a non-empty
    string, which is truthy — silently inverting every guard that reads it.
    """
    cfg = {
        "fpsProperties": {
            "conversationalService.conversationId": "conv-123",
            "conversationalService.endExchange": raw,
        }
    }
    config_path = tmp_path / "uipath.json"
    config_path.write_text(json.dumps(cfg))

    ctx = UiPathRuntimeContext.from_config(config_path=str(config_path))

    assert ctx.end_exchange is expected


def test_from_config_coerces_every_stringified_bool_fps_property(
    tmp_path: Path,
) -> None:
    """The coercion covers all bool-typed fps keys, not just endExchange."""
    cfg = {
        "fpsProperties": {
            "conversationalService.endExchange": "false",
            "conversationalService.enableOutputs": "false",
            "conversationalService.runAsMe": "false",
        }
    }
    config_path = tmp_path / "uipath.json"
    config_path.write_text(json.dumps(cfg))

    ctx = UiPathRuntimeContext.from_config(config_path=str(config_path))

    assert ctx.end_exchange is False
    assert ctx.conversational_outputs_enabled is False
    assert ctx.conversational_run_as_me is False


def test_from_config_leaves_non_bool_fps_properties_untouched(tmp_path: Path) -> None:
    """Only bool-typed targets are coerced; str fields keep their raw value."""
    cfg = {
        "fpsProperties": {
            "conversationalService.conversationId": "false",
            "conversationalService.exchangeId": "0",
        }
    }
    config_path = tmp_path / "uipath.json"
    config_path.write_text(json.dumps(cfg))

    ctx = UiPathRuntimeContext.from_config(config_path=str(config_path))

    assert ctx.conversation_id == "false"
    assert ctx.exchange_id == "0"


@pytest.mark.parametrize("raw", ["banana", "0", "1", "yes", "no", "off", "on"])
def test_from_config_passes_through_unrecognized_bool_fps_property(
    tmp_path: Path, raw: str
) -> None:
    """Only "true"/"false"/"" are parsed; anything else is left untouched.

    Coercing spellings a serializer never emits for a boolean would be guessing
    at intent, so unrecognized values keep the behavior they have always had.
    """
    cfg = {
        "fpsProperties": {
            "conversationalService.endExchange": raw,
        }
    }
    config_path = tmp_path / "uipath.json"
    config_path.write_text(json.dumps(cfg))

    ctx = UiPathRuntimeContext.from_config(config_path=str(config_path))

    assert ctx.end_exchange == raw


def test_from_config_still_accepts_real_bool_fps_property(tmp_path: Path) -> None:
    """A genuine JSON boolean keeps working unchanged."""
    cfg = {
        "fpsProperties": {
            "conversationalService.endExchange": False,
            "conversationalService.enableOutputs": True,
        }
    }
    config_path = tmp_path / "uipath.json"
    config_path.write_text(json.dumps(cfg))

    ctx = UiPathRuntimeContext.from_config(config_path=str(config_path))

    assert ctx.end_exchange is False
    assert ctx.conversational_outputs_enabled is True


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


@pytest.mark.parametrize(
    "command,expected",
    [
        ("run", "runtime"),
        ("debug", "playground"),
        ("dev", "playground"),
        ("eval", "eval"),
    ],
)
def test_constructor_derives_execution_source(command: str, expected: str) -> None:
    """execution_source is derived from the command on the plain constructor path."""
    ctx = UiPathRuntimeContext(command=command)

    assert ctx.execution_source == expected


@pytest.mark.parametrize(
    "command,expected",
    [
        ("run", "runtime"),
        ("eval", "eval"),
    ],
)
def test_with_defaults_derives_execution_source(
    command: str, expected: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """execution_source is also derived via with_defaults."""
    monkeypatch.chdir(tmp_path)

    ctx = UiPathRuntimeContext.with_defaults(command=command)

    assert ctx.execution_source == expected


def test_execution_source_unset_for_unmapped_command() -> None:
    """Commands that do not run an agent leave execution_source unset.

    The field must remain unset (not explicitly None) so it is absent from
    model_dump(exclude_unset=True).
    """
    ctx = UiPathRuntimeContext(command="pack")

    assert ctx.execution_source is None
    assert "execution_source" not in ctx.model_dump(exclude_unset=True)


def test_explicit_execution_source_not_overwritten() -> None:
    """An explicitly provided execution_source takes precedence over the command."""
    ctx = UiPathRuntimeContext(command="run", execution_source="custom")

    assert ctx.execution_source == "custom"


def test_constructor_accepts_maestro_flow_voice_mode() -> None:
    ctx = UiPathRuntimeContext(voice_mode="maestro_flow")

    assert ctx.voice_mode == "maestro_flow"


def test_from_config_accepts_maestro_flow_voice_mode(tmp_path: Path) -> None:
    config_path = tmp_path / "uipath.json"
    config_path.write_text(
        json.dumps({"fpsProperties": {"voice.mode": "maestro_flow"}})
    )

    ctx = UiPathRuntimeContext.from_config(str(config_path))

    assert ctx.voice_mode == "maestro_flow"


def test_from_config_maps_split_output_arguments(tmp_path: Path) -> None:
    """runtime.splitOutputArguments should map onto the knob."""
    cfg = {"runtime": {"splitOutputArguments": True}}
    config_path = tmp_path / "uipath.json"
    config_path.write_text(json.dumps(cfg))

    ctx = UiPathRuntimeContext.from_config(config_path=str(config_path))

    assert ctx.split_output_arguments is True


def test_split_output_arguments_defaults_off_when_config_key_absent(
    tmp_path: Path,
) -> None:
    """The split stays off when the config omits the key."""
    cfg = {"runtime": {"outputFile": "my_output.json"}}
    config_path = tmp_path / "uipath.json"
    config_path.write_text(json.dumps(cfg))

    ctx = UiPathRuntimeContext.from_config(config_path=str(config_path))

    assert ctx.split_output_arguments is False


def test_output_arguments_file_is_a_sibling_of_the_result_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The arguments file lands next to the result file, never in the process CWD.

    The host names the directory once, through runtime_dir, and both files follow
    it. The filename is not configurable, so the knob has exactly one encoding and
    the two files cannot be pointed at different directories.
    """
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    runtime_dir = tmp_path / "runtime"
    ctx = UiPathRuntimeContext(
        job_id="job-sibling",
        runtime_dir=str(runtime_dir),
        result_file="result.json",
        split_output_arguments=True,
    )

    arguments_path = Path(ctx.resolved_output_arguments_file_path)
    assert arguments_path.parent == Path(ctx.resolved_result_file_path).parent
    assert arguments_path.parent == runtime_dir
    assert arguments_path.name == "result.args.json"
    assert arguments_path.is_absolute()
    assert cwd not in arguments_path.parents


def test_output_arguments_file_cannot_collide_with_the_result_file(
    tmp_path: Path,
) -> None:
    """The suffix goes before the extension, so the two names can never converge.

    Naming the result file after the arguments file used to produce one path for
    both: the envelope overwrote the arguments and then pointed at itself.
    """
    ctx = UiPathRuntimeContext(
        job_id="job-collide",
        runtime_dir=str(tmp_path / "runtime"),
        result_file="output.args.json",
        split_output_arguments=True,
    )

    assert Path(ctx.resolved_output_arguments_file_path).name == "output.args.args.json"
    assert ctx.resolved_output_arguments_file_path != os.path.abspath(
        ctx.resolved_result_file_path
    )


def test_output_arguments_file_not_written_without_a_job(tmp_path: Path) -> None:
    """No job means no envelope, so the pointer would have no reader and no file.

    A local `uipath run` has no UIPATH_JOB_KEY; writing the payload there would
    leave a full copy on disk that nothing references.
    """
    runtime_dir = tmp_path / "runtime"
    ctx = UiPathRuntimeContext(
        runtime_dir=str(runtime_dir),
        result_file="result.json",
        split_output_arguments=True,
    )

    with ctx:
        ctx.result = UiPathRuntimeResult(
            status=UiPathRuntimeStatus.SUCCESSFUL,
            output={"foo": "bar"},
        )

    assert not Path(ctx.resolved_output_arguments_file_path).exists()
    assert not Path(ctx.resolved_result_file_path).exists()


def test_result_file_keeps_output_inline_when_split_disabled(
    tmp_path: Path,
) -> None:
    """Without the knob, the result file is byte-identical to the legacy envelope."""
    runtime_dir = tmp_path / "runtime"
    ctx = UiPathRuntimeContext(
        job_id="job-inline",
        runtime_dir=str(runtime_dir),
        result_file="result.json",
    )

    with ctx:
        ctx.result = UiPathRuntimeResult(
            status=UiPathRuntimeStatus.SUCCESSFUL,
            output={"foo": "bar"},
        )

    result_path = Path(ctx.resolved_result_file_path)
    # The envelope is written in text mode, so json's newline reaches disk as os.linesep
    expected = json.dumps(
        {"output": {"foo": "bar"}, "status": "successful"}, indent=2
    ).replace("\n", os.linesep)
    assert result_path.read_bytes() == expected.encode()

    content = json.loads(result_path.read_bytes())
    assert "outputArgumentsFilePath" not in content
    assert not Path(ctx.resolved_output_arguments_file_path).exists()


def test_output_arguments_written_to_separate_file(tmp_path: Path) -> None:
    """With the knob, the arguments move out and the envelope carries the path."""
    runtime_dir = tmp_path / "nested" / "runtime"
    ctx = UiPathRuntimeContext(
        job_id="job-split",
        runtime_dir=str(runtime_dir),
        result_file="result.json",
        split_output_arguments=True,
    )

    with ctx:
        ctx.result = UiPathRuntimeResult(
            status=UiPathRuntimeStatus.SUCCESSFUL,
            output={"foo": "bar"},
        )

    arguments_path = Path(ctx.resolved_output_arguments_file_path)
    # Parent directory is created on demand
    assert json.loads(arguments_path.read_text()) == {"foo": "bar"}

    content = json.loads(Path(ctx.resolved_result_file_path).read_text())
    assert "output" not in content
    assert content["status"] == UiPathRuntimeStatus.SUCCESSFUL.value
    assert content["outputArgumentsFilePath"] == str(arguments_path)
    assert Path(content["outputArgumentsFilePath"]).is_absolute()


def test_output_file_receives_bare_arguments_when_split_enabled(
    tmp_path: Path,
) -> None:
    """--output-file keeps receiving the bare arguments when both are set."""
    runtime_dir = tmp_path / "runtime"
    output_path = tmp_path / "output.json"
    ctx = UiPathRuntimeContext(
        job_id="job-both",
        runtime_dir=str(runtime_dir),
        result_file="result.json",
        output_file=str(output_path),
        split_output_arguments=True,
    )

    with ctx:
        ctx.result = UiPathRuntimeResult(
            status=UiPathRuntimeStatus.SUCCESSFUL,
            output={"foo": "bar"},
        )

    assert json.loads(output_path.read_text()) == {"foo": "bar"}
    arguments_path = Path(ctx.resolved_output_arguments_file_path)
    assert json.loads(arguments_path.read_text()) == {"foo": "bar"}


def test_faulted_run_keeps_status_and_error_inline_when_split_enabled(
    tmp_path: Path,
) -> None:
    """status and error stay in the envelope when the arguments are split out."""
    runtime_dir = tmp_path / "runtime"
    ctx = UiPathRuntimeContext(
        job_id="job-faulted-split",
        runtime_dir=str(runtime_dir),
        result_file="result.json",
        split_output_arguments=True,
    )

    with pytest.raises(RuntimeError, match="Stream blew up"):
        with ctx:
            raise RuntimeError("Stream blew up")

    content = json.loads(Path(ctx.resolved_result_file_path).read_text())
    assert content["status"] == UiPathRuntimeStatus.FAULTED.value
    assert content["error"]["code"] == "ERROR_RuntimeError"
    assert "Stream blew up" in content["error"]["detail"]
    assert "output" not in content

    # The pointer must never advertise a file that was not actually written
    arguments_path = Path(content["outputArgumentsFilePath"])
    assert arguments_path.exists()
    assert json.loads(arguments_path.read_text()) == {}


def test_resume_triggers_stay_inline_when_split_enabled(tmp_path: Path) -> None:
    """resume and resumeTriggers must never be moved out of the envelope.

    They are what makes a suspended job resumable, so a split that swept them
    into the arguments file would strand the job.
    """
    runtime_dir = tmp_path / "runtime"
    ctx = UiPathRuntimeContext(
        job_id="job-suspended-split",
        runtime_dir=str(runtime_dir),
        result_file="result.json",
        split_output_arguments=True,
    )

    trigger = UiPathResumeTrigger(item_key="k")
    with ctx:
        ctx.result = UiPathRuntimeResult(
            status=UiPathRuntimeStatus.SUSPENDED,
            output={"foo": "bar"},
            trigger=trigger,
            triggers=[trigger],
        )

    content = json.loads(Path(ctx.resolved_result_file_path).read_text())
    assert content["status"] == UiPathRuntimeStatus.SUSPENDED.value
    assert content["resume"]["itemKey"] == "k"
    assert len(content["resumeTriggers"]) == 1
    assert content["resumeTriggers"][0]["itemKey"] == "k"
    # Only the output moved out
    assert "output" not in content
    arguments_path = Path(content["outputArgumentsFilePath"])
    assert json.loads(arguments_path.read_text()) == {"foo": "bar"}


def test_failed_arguments_write_faults_the_run(tmp_path: Path) -> None:
    """A failing arguments write faults the run, like every other write in __exit__.

    Falling back to the inline value would write the same bytes to the same volume,
    so it cannot rescue the failure that actually matters, and it would hand the
    consumer the payload the split exists to keep out of its heap.
    """
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    # A directory cannot be opened for writing, so the split write fails
    (runtime_dir / "result.args.json").mkdir()
    ctx = UiPathRuntimeContext(
        job_id="job-failed-write",
        runtime_dir=str(runtime_dir),
        result_file="result.json",
        split_output_arguments=True,
    )

    with pytest.raises(RuntimeError) as excinfo:
        with ctx:
            ctx.result = UiPathRuntimeResult(
                status=UiPathRuntimeStatus.SUCCESSFUL,
                output={"foo": "bar"},
            )

    assert "RUNTIME_SHUTDOWN_ERROR" in str(excinfo.value)

    content = json.loads(Path(ctx.resolved_result_file_path).read_text())
    assert content["status"] == UiPathRuntimeStatus.FAULTED.value
    assert content["error"]["code"] == "RUNTIME_SHUTDOWN_ERROR"
