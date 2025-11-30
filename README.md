# UiPath Runtime

[![PyPI downloads](https://img.shields.io/pypi/dm/uipath-runtime.svg)](https://pypi.org/project/uipath-runtime/)
[![PyPI - Version](https://img.shields.io/pypi/v/uipath-runtime)](https://pypi.org/project/uipath-runtime/)
[![Python versions](https://img.shields.io/pypi/pyversions/uipath-runtime.svg)](https://pypi.org/project/uipath-runtime/)

Runtime abstractions and contracts for the UiPath Python SDK.

## Overview

`uipath-runtime` provides the foundational interfaces and base contracts for building agent runtimes in the UiPath ecosystem. It defines the protocols that all runtime implementations must follow and provides utilities for execution context, event streaming, tracing, structured error handling, durable execution, and human-in-the-loop interactions.

This package is typically used as a dependency by higher-level SDKs such as:
- [`uipath`](https://github.com/uipath/uipath-python) [![PyPI](https://img.shields.io/pypi/v/uipath?label=pypi)](https://pypi.org/project/uipath/) [![Downloads](https://img.shields.io/pypi/dm/uipath?label=)](https://pypi.org/project/uipath/) [![GitHub](https://img.shields.io/github/stars/uipath/uipath-python?style=social)](https://github.com/uipath/uipath-python)
- [`uipath-langchain`](https://github.com/uipath/uipath-langchain-python) [![PyPI](https://img.shields.io/pypi/v/uipath-langchain?label=pypi)](https://pypi.org/project/uipath-langchain/) [![Downloads](https://img.shields.io/pypi/dm/uipath-langchain?label=)](https://pypi.org/project/uipath-langchain/) [![GitHub](https://img.shields.io/github/stars/uipath/uipath-langchain-python?style=social)](https://github.com/uipath/uipath-langchain-python)
- [`uipath-llamaindex`](https://github.com/uipath/uipath-llamaindex-python) [![PyPI](https://img.shields.io/pypi/v/uipath-llamaindex?label=pypi)](https://pypi.org/project/uipath-llamaindex/) [![Downloads](https://img.shields.io/pypi/dm/uipath-llamaindex?label=)](https://pypi.org/project/uipath-llamaindex/) [![GitHub](https://img.shields.io/github/stars/uipath/uipath-llamaindex-python?style=social)](https://github.com/uipath/uipath-llamaindex-python)
- [`uipath-mcp`](https://github.com/uipath/uipath-mcp-python) [![PyPI](https://img.shields.io/pypi/v/uipath-mcp?label=pypi)](https://pypi.org/project/uipath-mcp/) [![Downloads](https://img.shields.io/pypi/dm/uipath-mcp?label=)](https://pypi.org/project/uipath-mcp/) [![GitHub](https://img.shields.io/github/stars/uipath/uipath-mcp-python?style=social)](https://github.com/uipath/uipath-mcp-python)

You would use this directly only if you're building custom runtime implementations.

## Installation

```bash
uv add uipath-runtime
```

## Developer Tools

Check out [`uipath-dev`](https://github.com/uipath/uipath-dev-python) - an interactive terminal application for building, testing, and debugging UiPath Python runtimes, agents, and automation scripts.

## Runtime Protocols

All runtimes implement the `UiPathRuntimeProtocol` (or one of its sub-protocols):

- `get_schema()` — defines input and output JSON schemas.
- `execute(input, options)` — executes the runtime logic and returns a `UiPathRuntimeResult`.
- `stream(input, options)` — optionally streams runtime events for real-time monitoring.
- `dispose()` — releases resources when the runtime is no longer needed.

Any class that structurally implements these methods satisfies the protocol.

```python
from typing import Any, AsyncGenerator, Optional

from uipath.runtime import (
    UiPathRuntimeResult,
    UiPathRuntimeStatus,
    UiPathRuntimeSchema,
    UiPathRuntimeEvent,
    UiPathExecuteOptions,
    UiPathStreamOptions,
)
from uipath.runtime.events import UiPathRuntimeStateEvent


class MyRuntime:
    """Example runtime implementing the UiPath runtime protocols."""

    async def get_schema(self) -> UiPathRuntimeSchema:
        return UiPathRuntimeSchema(
            input={
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
            output={
                "type": "object",
                "properties": {"result": {"type": "string"}},
                "required": ["result"],
            },
        )

    async def execute(
        self,
        input: Optional[dict[str, Any]] = None,
        options: Optional[UiPathExecuteOptions] = None,
    ) -> UiPathRuntimeResult:
        message = (input or {}).get("message", "")
        return UiPathRuntimeResult(
            output={'message': 'Hello from MyRuntime'},
            status=UiPathRuntimeStatus.SUCCESSFUL,
        )

    async def stream(
        self,
        input: Optional[dict[str, Any]] = None,
        options: Optional[UiPathStreamOptions] = None,
    ) -> AsyncGenerator[UiPathRuntimeEvent, None]:
        yield UiPathRuntimeStateEvent(payload={"status": "starting"})
        yield UiPathRuntimeResult(
            output={"completed": True},
            status=UiPathRuntimeStatus.SUCCESSFUL,
        )

    async def dispose(self) -> None:
        pass
```


## Event Streaming

Runtimes can optionally emit real-time events during execution:

```python
from uipath.runtime.events import (
    UiPathRuntimeStateEvent,
    UiPathRuntimeMessageEvent,
)
from uipath.runtime.result import UiPathRuntimeResult

async for event in runtime.stream({"query": "hello"}):
    if isinstance(event, UiPathRuntimeStateEvent):
        print(f"State update: {event.payload}")
    elif isinstance(event, UiPathRuntimeMessageEvent):
        print(f"Message received: {event.payload}")
    elif isinstance(event, UiPathRuntimeResult):
        print(f"Completed: {event.output}")
```

If a runtime doesn’t support streaming, it raises a `UiPathStreamNotSupportedError`.


## Structured Error Handling

Runtime errors use a consistent, structured model:

```python
from uipath.runtime.errors import UiPathRuntimeError, UiPathErrorCode, UiPathErrorCategory

raise UiPathRuntimeError(
    UiPathErrorCode.EXECUTION_ERROR,
    "Agent failed",
    "Failed to call external service",
    UiPathErrorCategory.USER,
)
```

Resulting JSON contract:

```json
{
  "code": "Python.EXECUTION_ERROR",
  "title": "Agent failed",
  "detail": "Failed to call external service",
  "category": "User"
}
```

## Runtime Factory

`UiPathRuntimeFactoryProtocol` provides a consistent contract for discovering and creating runtime instances.

Factories decouple runtime construction (configuration, dependencies) from runtime execution, allowing orchestration, discovery, reuse, and tracing across multiple types of runtimes.

```python
from typing import Any, AsyncGenerator, Optional

from uipath.runtime import (
    UiPathRuntimeResult,
    UiPathRuntimeStatus,
    UiPathRuntimeSchema,
    UiPathExecuteOptions,
    UiPathStreamOptions,
    UiPathRuntimeProtocol,
    UiPathRuntimeFactoryProtocol
)

class MyRuntimeFactory:
    async def new_runtime(self, entrypoint: str, runtime_id: str) -> UiPathRuntimeProtocol:
        return MyRuntime()

    async def discover_runtimes(self) -> list[UiPathRuntimeProtocol]:
        return []

    def discover_entrypoints(self) -> list[str]:
        return []


factory = MyRuntimeFactory()
runtime = await factory.new_runtime("example", "id")

result = await runtime.execute()
print(result.output)  # {'message': 'Hello from MyRuntime'}
```

## Execution Context

`UiPathRuntimeContext` manages configuration, file I/O, and logs across runtime execution.
It can read JSON input files, capture all stdout/stderr logs, and automatically write output and result files when execution completes.

```python
from uipath.runtime import UiPathRuntimeContext, UiPathRuntimeResult, UiPathRuntimeStatus

with UiPathRuntimeContext(input_file="input.json", result_file="result.json", logs_file="execution.log") as ctx:
    ctx.result = await runtime.execute(ctx.input)
# On exit: the result and logs are written automatically to the configured files
```

When execution fails, the context:
- Writes a structured error contract to the result file.
- Re-raises the original exception.


## Execution Runtime

`UiPathExecutionRuntime` wraps any runtime with tracing, telemetry, and log collection capabilities. When running multiple runtimes in the same process, this wrapper ensures each execution's spans and logs are properly isolated and captured.

```mermaid
graph TB
    TM[TraceManager<br/>Shared across all runtimes]

    FACTORY[Factory]

    RT[Runtime]

    EXE[ExecutionRuntime<br/>exec-id: exec-id]

    %% Factory creates runtimes
    FACTORY -->|new_runtime| RT

    %% Runtimes wrapped by ExecutionRuntime
    RT -->|wrapped by| EXE

    %% TraceManager shared with all
    TM -.->|shared| EXE

    %% Execution captures spans to TraceManager
    EXE -->|captures spans| TM

    %% Styling
    style TM fill:#e1f5ff,stroke:#0277bd,stroke-width:3px
    style FACTORY fill:#f3e5f5
    style RT fill:#fff3e0
    style EXE fill:#e8f5e9
```

```python
from uipath.core import UiPathTraceManager
from uipath.runtime import UiPathExecutionRuntime

trace_manager = UiPathTraceManager()
runtime = MyRuntime()
executor = UiPathExecutionRuntime(
    runtime,
    trace_manager,
    root_span="my-runtime",
    execution_id="exec-123",
)

result = await executor.execute({"message": "hello"})
spans = trace_manager.get_execution_spans("exec-123")  # captured spans
logs = executor.log_handler.buffer  # captured logs
print(result.output)  # {'message': 'Hello from MyRuntime'}
```

## Example: Runtime Orchestration

This example demonstrates an **orchestrator runtime** that receives a `UiPathRuntimeFactoryProtocol`, creates child runtimes through it, and executes each one via `UiPathExecutionRuntime`, all within a single shared `UiPathTraceManager`.

<details>
 <summary>Orchestrator Runtime</summary>

```python
from typing import Any, Optional, AsyncGenerator

from uipath.core import UiPathTraceManager
from uipath.runtime import (
    UiPathExecutionRuntime,
    UiPathRuntimeResult,
    UiPathRuntimeStatus,
    UiPathExecuteOptions,
    UiPathStreamOptions,
    UiPathRuntimeProtocol,
    UiPathRuntimeFactoryProtocol
)


class ChildRuntime:
    """A simple child runtime that echoes its name and input."""

    def __init__(self, name: str):
        self.name = name

    async def get_schema(self):
        return None

    async def execute(
        self,
        input: Optional[dict[str, Any]] = None,
        options: Optional[UiPathExecuteOptions] = None,
    ) -> UiPathRuntimeResult:
        payload = input or {}
        return UiPathRuntimeResult(
            output={
                "runtime": self.name,
                "input": payload,
            },
            status=UiPathRuntimeStatus.SUCCESSFUL,
        )

    async def stream(
        self,
        input: Optional[dict[str, Any]] = None,
        options: Optional[UiPathStreamOptions] = None,
    ) -> AsyncGenerator[UiPathRuntimeResult, None]:
        yield await self.execute(input, options)

    async def dispose(self) -> None:
        pass


class ChildRuntimeFactory:
    """Factory that creates ChildRuntime instances."""

    async def new_runtime(self, entrypoint: str) -> UiPathRuntimeProtocol:
        return ChildRuntime(name=entrypoint)

    async def discover_runtimes(self) -> list[UiPathRuntimeProtocol]:
        return []

    def discover_entrypoints(self) -> list[str]:
        return []


class OrchestratorRuntime:
    """A runtime that orchestrates multiple child runtimes via a factory."""

    def __init__(
        self,
        factory: UiPathRuntimeFactoryProtocol,
        trace_manager: UiPathTraceManager,
    ):
        self.factory = factory
        self.trace_manager = trace_manager

    async def get_schema(self):
        return None

    async def execute(
        self,
        input: Optional[dict[str, Any]] = None,
        options: Optional[UiPathExecuteOptions] = None,
    ) -> UiPathRuntimeResult:
        payload = input or {}
        child_inputs: list[dict[str, Any]] = payload.get("children", [])
        child_results: list[dict[str, Any]] = []

        for i, child_input in enumerate(child_inputs):
            # Use the factory to create a new child runtime
            child_runtime = await self.factory.new_runtime(entrypoint=f"child-{i}", runtime_id=f"child-{i}")

            # Wrap child runtime with tracing + logs
            execution_id = f"child-{i}"
            executor = UiPathExecutionRuntime(
                delegate=child_runtime,
                trace_manager=self.trace_manager,
                root_span=f"child-span-{i}",
                execution_id=execution_id,
            )

            # Execute child runtime
            result = await executor.execute(child_input, options=options)
            child_results.append(result.output or {})
            child_spans = trace_manager.get_execution_spans(execution_id) # Captured spans
            # Dispose the child runtime when finished
            await child_runtime.dispose()

        return UiPathRuntimeResult(
            output={
                "main": True,
                "children": child_results,
            },
            status=UiPathRuntimeStatus.SUCCESSFUL,
        )

    async def stream(
        self,
        input: Optional[dict[str, Any]] = None,
        options: Optional[UiPathStreamOptions] = None,
    ) -> AsyncGenerator[UiPathRuntimeResult, None]:
        yield await self.execute(input, options)

    async def dispose(self) -> None:
        pass


# Example usage
async def main() -> None:
    trace_manager = UiPathTraceManager()
    factory = ChildRuntimeFactory()
    options = UiPathExecuteOptions()

    with UiPathRuntimeContext(job_id="main-job-001") as ctx:
        runtime = OrchestratorRuntime(factory=factory, trace_manager=trace_manager)

        input_data = {
            "children": [
                {"message": "hello from child 1"},
                {"message": "hello from child 2"},
            ]
        }

        ctx.result = await runtime.execute(input=input_data, options=options)
        print(ctx.result.output)

# Output:
# {
#   "main": True,
#   "children": [
#       {"runtime": "child-0", "input": {"message": "hello from child 1"}},
#       {"runtime": "child-1", "input": {"message": "hello from child 2"}}
#   ]
# }
```

</details>
