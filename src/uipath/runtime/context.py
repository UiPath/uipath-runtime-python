"""Context information passed throughout the runtime execution."""

import json
import logging
import os
from functools import cached_property
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict
from uipath.core.errors import UiPathFaultedTriggerError
from uipath.core.tracing import UiPathTraceManager

from uipath.runtime.errors import (
    UiPathErrorCategory,
    UiPathErrorCode,
    UiPathErrorContract,
    UiPathRuntimeError,
)
from uipath.runtime.logging._interceptor import UiPathRuntimeLogsInterceptor
from uipath.runtime.result import UiPathRuntimeResult, UiPathRuntimeStatus

logger = logging.getLogger(__name__)


class UiPathRuntimeContext(BaseModel):
    """Context information passed throughout the runtime execution."""

    entrypoint: str | None = None
    input: str | None = None
    resume: bool = False
    command: str | None = None
    job_id: str | None = None
    conversation_id: str | None = None
    exchange_id: str | None = None
    message_id: str | None = None
    mcp_server_id: str | None = None
    mcp_server_slug: str | None = None
    tenant_id: str | None = None
    org_id: str | None = None
    folder_key: str | None = None
    process_key: str | None = None
    config_path: str = "uipath.json"
    runtime_dir: str | None = "__uipath"
    result_file: str = "output.json"
    state_file: str = "state.db"
    input_file: str | None = None
    output_file: str | None = None
    trace_file: str | None = None
    logs_file: str | None = "execution.log"
    logs_min_level: str | None = "INFO"
    result: UiPathRuntimeResult | None = None
    trace_manager: UiPathTraceManager | None = None

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="allow")

    def get_input(self) -> dict[str, Any] | None:
        """Get parsed input data.

        Priority:
        1. If input_file is specified, read and parse from file
        2. Otherwise, parse the input string

        Returns:
            Parsed input dictionary

        Raises:
            UiPathRuntimeError: If JSON is invalid or file not found
        """
        if self.input_file:
            return self._read_input_from_file(self.input_file)

        return self._parse_input_string(self.input)

    def _read_input_from_file(self, file_path: str) -> dict[str, Any]:
        """Read and parse input from JSON file."""
        path = Path(file_path)

        # Validate file extension
        if path.suffix != ".json":
            raise UiPathRuntimeError(
                code=UiPathErrorCode.INVALID_INPUT_FILE_EXTENSION,
                title="Invalid Input File Extension",
                detail=f"The provided input file must be in JSON format. Got: {path.suffix}",
                category=UiPathErrorCategory.USER,
            )

        # Check if file exists
        if not path.exists():
            raise UiPathRuntimeError(
                code=UiPathErrorCode.MISSING_INPUT_FILE,
                title="Input File Not Found",
                detail=f"The input file does not exist: {file_path}",
                category=UiPathErrorCategory.USER,
            )

        try:
            with open(path) as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            raise UiPathRuntimeError(
                code=UiPathErrorCode.INPUT_INVALID_JSON,
                title="Invalid JSON in Input File",
                detail=f"The input file contains invalid JSON: {e}",
                category=UiPathErrorCategory.USER,
            ) from e
        except Exception as e:
            raise UiPathRuntimeError(
                code=UiPathErrorCode.INPUT_INVALID_JSON,
                title="Failed to Read Input File",
                detail=f"Error reading input file: {e}",
                category=UiPathErrorCategory.SYSTEM,
            ) from e

    def _parse_input_string(self, input_str: str | None) -> dict[str, Any] | None:
        """Parse input from JSON string."""
        if not input_str or input_str.strip() == "":
            return None

        try:
            parsed = json.loads(input_str)

            # Ensure we return a dict
            if not isinstance(parsed, dict):
                raise UiPathRuntimeError(
                    code=UiPathErrorCode.INPUT_INVALID_JSON,
                    title="Invalid Input Type",
                    detail=f"Input must be a JSON object, got: {type(parsed).__name__}",
                    category=UiPathErrorCategory.USER,
                )

            return parsed

        except json.JSONDecodeError as e:
            raise UiPathRuntimeError(
                code=UiPathErrorCode.INPUT_INVALID_JSON,
                title="Invalid JSON Input",
                detail=f"The input data is not valid JSON: {e}",
                category=UiPathErrorCategory.USER,
            ) from e

    def __enter__(self):
        """Enter method called when entering the 'async with' block.

        Initializes and prepares the runtime contextual environment.

        Returns:
            The runtime context instance
        """
        # Intercept all stdout/stderr/logs
        # Write to file (runtime), stdout (debug) or log handler (if provided)
        self.logs_interceptor = UiPathRuntimeLogsInterceptor(
            min_level=self.logs_min_level,
            dir=self.runtime_dir,
            file=self.logs_file,
            job_id=self.job_id,
        )
        self.logs_interceptor.setup()

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Async exit method called when exiting the 'async with' block.

        Cleans up resources and handles any exceptions.

        Always writes output file regardless of whether execution was successful,
        suspended, or encountered an error.
        """
        try:
            if self.result is None:
                self.result = UiPathRuntimeResult()

            if exc_type:
                # Create error info from exception
                match exc_type:
                    case UiPathFaultedTriggerError():
                        error_info = UiPathRuntimeError.from_resume_trigger_error(
                            exc_type
                        ).error_info
                    case UiPathRuntimeError():
                        error_info = exc_val.error_info
                    case _:
                        # Generic error
                        error_info = UiPathErrorContract(
                            code=f"ERROR_{exc_type.__name__}",
                            title=f"Runtime error: {exc_type.__name__}",
                            detail=str(exc_val),
                            category=UiPathErrorCategory.UNKNOWN,
                        )

                self.result.status = UiPathRuntimeStatus.FAULTED
                self.result.error = error_info

            content = self.result.to_dict()

            # Always write output file at runtime, except for inner runtimes
            # Inner runtimes have execution_id
            if self.job_id:
                with open(self.result_file_path, "w") as f:
                    json.dump(content, f, indent=2, default=str)

            # Write the execution output to file if requested
            if self.output_file:
                output_payload = content.get("output", {})
                with open(self.output_file, "w") as f:
                    json.dump(output_payload, f, default=str)

            # Don't suppress exceptions
            return False

        except Exception as e:
            logger.error(f"Error during runtime shutdown: {str(e)}")

            # Create a fallback error result if we fail during cleanup
            if not isinstance(e, UiPathRuntimeError):
                error_info = UiPathErrorContract(
                    code="RUNTIME_SHUTDOWN_ERROR",
                    title="Runtime shutdown failed",
                    detail=f"Error: {str(e)}",
                    category=UiPathErrorCategory.SYSTEM,
                )
            else:
                error_info = e.error_info

            # Last-ditch effort to write error output
            try:
                error_result = UiPathRuntimeResult(
                    status=UiPathRuntimeStatus.FAULTED, error=error_info
                )
                error_result_content = error_result.to_dict()
                if self.job_id:
                    with open(self.result_file_path, "w") as f:
                        json.dump(error_result_content, f, indent=2, default=str)
            except Exception as write_error:
                logger.error(f"Failed to write error output file: {str(write_error)}")
                raise

            # Re-raise as RuntimeError if it's not already a UiPathRuntimeError
            if not isinstance(e, UiPathRuntimeError):
                raise RuntimeError(
                    error_info.code,
                    error_info.title,
                    error_info.detail,
                    error_info.category,
                ) from e
            raise
        finally:
            # Restore original logging
            if hasattr(self, "logs_interceptor"):
                self.logs_interceptor.teardown()

    @cached_property
    def result_file_path(self) -> str:
        """Get the full path to the result file."""
        if self.runtime_dir and self.result_file:
            os.makedirs(self.runtime_dir, exist_ok=True)
            return os.path.join(self.runtime_dir, self.result_file)
        return os.path.join("__uipath", "output.json")

    @cached_property
    def state_file_path(self) -> str:
        """Get the full path to the state file."""
        if self.runtime_dir and self.state_file:
            os.makedirs(self.runtime_dir, exist_ok=True)
            return os.path.join(self.runtime_dir, self.state_file)
        return os.path.join("__uipath", "state.db")

    @classmethod
    def with_defaults(
        cls, config_path: str | None = None, **kwargs
    ) -> "UiPathRuntimeContext":
        """Construct a context with defaults, reading env vars and config file."""
        resolved_config_path = config_path or os.environ.get(
            "UIPATH_CONFIG_PATH", "uipath.json"
        )

        base = cls.from_config(resolved_config_path)
        base.config_path = resolved_config_path

        bool_map = {"true": True, "false": False}
        tracing_enabled = os.environ.get("UIPATH_TRACING_ENABLED", True)
        if isinstance(tracing_enabled, str) and tracing_enabled.lower() in bool_map:
            tracing_enabled = bool_map[tracing_enabled.lower()]

        # Apply defaults from env
        base.job_id = os.environ.get("UIPATH_JOB_KEY")
        base.logs_min_level = os.environ.get("LOG_LEVEL", "INFO")
        base.org_id = os.environ.get("UIPATH_ORGANIZATION_ID")
        base.tenant_id = os.environ.get("UIPATH_TENANT_ID")
        base.process_key = os.environ.get("UIPATH_PROCESS_UUID")
        base.folder_key = os.environ.get("UIPATH_FOLDER_KEY")

        # Override with kwargs
        for k, v in kwargs.items():
            setattr(base, k, v)

        return base

    @classmethod
    def from_config(
        cls, config_path: str | None = None, **kwargs
    ) -> "UiPathRuntimeContext":
        """Load configuration from uipath.json file."""
        path = config_path or "uipath.json"
        config: dict[str, Any] = {}

        if os.path.exists(path):
            with open(path, "r") as f:
                config = json.load(f)

        instance = cls()

        mapping = {
            "dir": "runtime_dir",
            "outputFile": "result_file",  # we need this to maintain back-compat with serverless runtime
            "stateFile": "state_file",
            "logsFile": "logs_file",
        }

        fps_mappings = {
            "conversationalService.conversationId": "conversation_id",
            "conversationalService.exchangeId": "exchange_id",
            "conversationalService.messageId": "message_id",
            "mcpServer.id": "mcp_server_id",
            "mcpServer.slug": "mcp_server_slug",
        }

        attributes_set = set()

        runtime_config = config.get("runtime", {})
        fps_config = config.get("fpsProperties", {})

        if runtime_config or fps_config:
            # Handle runtime mapping
            for config_key, attr_name in mapping.items():
                if config_key in runtime_config and hasattr(instance, attr_name):
                    attributes_set.add(attr_name)
                    setattr(instance, attr_name, runtime_config[config_key])

            # Handle fpsProperties mapping
            for config_key, attr_name in fps_mappings.items():
                if config_key in fps_config and hasattr(instance, attr_name):
                    attributes_set.add(attr_name)
                    setattr(instance, attr_name, fps_config[config_key])

        for _, attr_name in mapping.items():
            if attr_name in kwargs and hasattr(instance, attr_name):
                if attr_name not in attributes_set:
                    setattr(instance, attr_name, kwargs[attr_name])

        return instance
