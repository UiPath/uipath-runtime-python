"""Context information passed throughout the runtime execution."""

import json
import logging
import os
from functools import cached_property
from typing import (
    Any,
    List,
    Literal,
    Optional,
    TypeVar,
)
from uuid import uuid4

from pydantic import BaseModel
from uipath.core.tracing.context import UiPathTraceContext

from uipath.runtime.result import UiPathRuntimeResult

C = TypeVar("C", bound="UiPathRuntimeContext")


class UiPathRuntimeContext(BaseModel):
    """Context information passed throughout the runtime execution."""

    entrypoint: Optional[str] = None
    input: Optional[Any] = None
    resume: bool = False
    job_id: Optional[str] = None
    execution_id: Optional[str] = None
    trace_context: Optional[UiPathTraceContext] = None
    config_path: str = "uipath.json"
    runtime_dir: Optional[str] = "__uipath"
    result_file: str = "output.json"
    state_file: str = "state.db"
    input_file: Optional[str] = None
    output_file: Optional[str] = None
    trace_file: Optional[str] = None
    logs_file: Optional[str] = "execution.log"
    log_handler: Optional[logging.Handler] = None
    logs_min_level: Optional[str] = "INFO"
    breakpoints: Optional[List[str] | Literal["*"]] = None
    result: Optional[UiPathRuntimeResult] = None

    model_config = {"arbitrary_types_allowed": True, "extra": "allow"}

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

        base.trace_context = UiPathTraceContext(
            trace_id=os.environ.get("UIPATH_TRACE_ID"),
            parent_span_id=os.environ.get("UIPATH_PARENT_SPAN_ID"),
            root_span_id=os.environ.get("UIPATH_ROOT_SPAN_ID"),
            enabled=tracing_enabled,
            job_id=os.environ.get("UIPATH_JOB_KEY"),
            org_id=os.environ.get("UIPATH_ORGANIZATION_ID"),
            tenant_id=os.environ.get("UIPATH_TENANT_ID"),
            process_key=os.environ.get("UIPATH_PROCESS_UUID"),
            folder_key=os.environ.get("UIPATH_FOLDER_KEY"),
            reference_id=os.environ.get("UIPATH_JOB_KEY") or str(uuid4()),
        )

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
