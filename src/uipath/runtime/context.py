"""Context information passed throughout the runtime execution."""

import json
import logging
import os
from functools import cached_property
from typing import (
    Any,
    Optional,
    TypeVar,
)

from pydantic import BaseModel

from uipath.runtime.errors import (
    UiPathErrorCategory,
    UiPathErrorCode,
    UiPathErrorContract,
    UiPathRuntimeError,
)
from uipath.runtime.logging._interceptor import UiPathRuntimeLogsInterceptor
from uipath.runtime.result import UiPathRuntimeResult, UiPathRuntimeStatus

logger = logging.getLogger(__name__)

C = TypeVar("C", bound="UiPathRuntimeContext")


class UiPathRuntimeContext(BaseModel):
    """Context information passed throughout the runtime execution."""

    entrypoint: Optional[str] = None
    input: Optional[dict[str, Any]] = None
    job_id: Optional[str] = None
    config_path: str = "uipath.json"
    runtime_dir: Optional[str] = "__uipath"
    result_file: str = "output.json"
    state_file: str = "state.db"
    input_file: Optional[str] = None
    output_file: Optional[str] = None
    trace_file: Optional[str] = None
    logs_file: Optional[str] = "execution.log"
    logs_min_level: Optional[str] = "INFO"
    result: Optional[UiPathRuntimeResult] = None

    model_config = {"arbitrary_types_allowed": True, "extra": "allow"}

    def __enter__(self):
        """Async enter method called when entering the 'async with' block.

        Initializes and prepares the runtime contextual environment.

        Returns:
            The runtime context instance
        """
        try:
            if self.input_file:
                # Read the input from file if provided
                _, file_extension = os.path.splitext(self.input_file)
                if file_extension != ".json":
                    raise UiPathRuntimeError(
                        code=UiPathErrorCode.INVALID_INPUT_FILE_EXTENSION,
                        title="Invalid Input File Extension",
                        detail="The provided input file must be in JSON format.",
                    )
                with open(self.input_file) as f:
                    self.input = json.loads(f.read())
        except json.JSONDecodeError as e:
            raise UiPathRuntimeError(
                UiPathErrorCode.INPUT_INVALID_JSON,
                "Invalid JSON input",
                f"The input data is not valid JSON: {str(e)}",
                UiPathErrorCategory.USER,
            ) from e

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
                if isinstance(exc_val, UiPathRuntimeError):
                    error_info = exc_val.error_info
                else:
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
    def with_defaults(cls: type[C], config_path: Optional[str] = None, **kwargs) -> C:
        """Construct a context with defaults, reading env vars and config file."""
        resolved_config_path = config_path or os.environ.get(
            "UIPATH_CONFIG_PATH", "uipath.json"
        )

        base = cls.from_config(resolved_config_path)

        bool_map = {"true": True, "false": False}
        tracing_enabled = os.environ.get("UIPATH_TRACING_ENABLED", True)
        if isinstance(tracing_enabled, str) and tracing_enabled.lower() in bool_map:
            tracing_enabled = bool_map[tracing_enabled.lower()]

        # Apply defaults from env
        base.job_id = os.environ.get("UIPATH_JOB_KEY")
        base.logs_min_level = os.environ.get("LOG_LEVEL", "INFO")

        # Override with kwargs
        for k, v in kwargs.items():
            setattr(base, k, v)

        return base

    @classmethod
    def from_config(cls: type[C], config_path: Optional[str] = None, **kwargs) -> C:
        """Load configuration from uipath.json file."""
        path = config_path or "uipath.json"
        config = {}

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

        attributes_set = set()
        if "runtime" in config:
            runtime_config = config["runtime"]
            for config_key, attr_name in mapping.items():
                if config_key in runtime_config and hasattr(instance, attr_name):
                    attributes_set.add(attr_name)
                    setattr(instance, attr_name, runtime_config[config_key])

        for _, attr_name in mapping.items():
            if attr_name in kwargs and hasattr(instance, attr_name):
                if attr_name not in attributes_set:
                    setattr(instance, attr_name, kwargs[attr_name])

        return instance
