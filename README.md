# UiPath Runtime

[![PyPI downloads](https://img.shields.io/pypi/dm/uipath-runtime.svg)](https://pypi.org/project/uipath-runtime/)
[![Python versions](https://img.shields.io/pypi/pyversions/uipath-runtime.svg)](https://pypi.org/project/uipath-runtime/)

Core runtime abstractions and contracts for the UiPath Python SDK.

## Overview

`uipath-runtime` provides the foundational interfaces and base classes for building agent runtimes in the UiPath ecosystem. It defines the contracts that all runtime implementations must follow, handles execution context, event streaming, tracing, and error management.

This package is typically used as a dependency by higher-level packages like `uipath-langchain`, `uipath-llamaindex`, or the main `uipath` SDK. You would use this directly only if you're building custom runtime implementations.

## Installation

```bash
uv add uipath-runtime
```

## Core Concepts

### Runtime Base Class

All runtimes extend `UiPathBaseRuntime` and implement these core methods:

```python
from uipath.runtime import UiPathBaseRuntime, UiPathRuntimeContext, UiPathRuntimeResult, UiPathRuntimeEvent

class MyCustomRuntime(UiPathBaseRuntime):
    def __init__(self, context: UiPathRuntimeContext):
        super().__init__(context)

    async def get_schema(self) -> UiPathRuntimeSchema:
        # Returns the runtime's JSON schemas
        return UiPathRuntimeSchema(
            input={
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "Input message"
                    }
                },
                "required": ["message"]
            },
            output={
                "type": "object",
                "properties": {
                    "result": {
                        "type": "string",
                        "description": "Execution result"
                    }
                },
                "required": ["result"]
            }
        )

    async def execute(self) -> UiPathRuntimeResult:
        # Execute framework-specific agent invoke logic
        return UiPathRuntimeResult(
            output={"result": "success"},
            status=UiPathRuntimeStatus.SUCCESSFUL
        )

    async def stream(
        self,
    ) -> AsyncGenerator[UiPathRuntimeEvent, None]:
        # Stream events during execution for real-time monitoring
        yield UiPathRuntimeStateEvent(
            payload={"status": "starting"},
            execution_id=self.context.execution_id
        )

        # Yield final result
        yield UiPathRuntimeResult(
            output={"completed": True},
            status=UiPathRuntimeStatus.SUCCESSFUL
        )

    async def validate(self) -> None:
        # Validate configuration before execution
        if not self.context.entrypoint:
            raise UiPathRuntimeError(
                UiPathErrorCode.ENTRYPOINT_MISSING,
                "Missing entrypoint",
                "Detailed error message here",
                UiPathErrorCategory.USER,
            )

    async def cleanup(self) -> None:
        # Clean up resources after execution
        pass
```

### Runtime Factory

The factory pattern handles runtime instantiation, instrumentation, and tracing:

```python
from uipath.runtime import UiPathRuntimeFactory, UiPathRuntimeContext, UiPathRuntimeExecutor

factory = UiPathRuntimeFactory(MyCustomRuntime)

executor = UiPathRuntimeExecutor()

# Add OpenTelemetry instrumentation
executor.add_instrumentor(MyInstrumentor, get_current_span)

# Add span exporters for tracing
executor.add_span_exporter(JsonLinesFileExporter("trace.jsonl"))

# Execute
context = UiPathRuntimeContext(entrypoint="main.py", input='{"query": "hello"}')
async with factory.from_context(context):
    result = await executor.execute(runtime)
```

### Event Streaming

Runtimes can stream events during execution for real-time monitoring:

```python
async for event in executor.stream(runtime):
    if isinstance(event, UiPathRuntimeStateEvent):
        print(f"State update: {event.payload}")
    elif isinstance(event, UiPathRuntimeMessageEvent):
        print(f"Message: {event.payload}")
    elif isinstance(event, UiPathRuntimeResult):
        print(f"Completed: {event.output}")
```

### Execution Context

Runtime context carries configuration and state throughout execution:

```python
context = UiPathRuntimeContext(
    entrypoint="agent.py",
    input='{"query": "hello"}',
    job_id="job-123",
    resume=False,
)
```

### Error Handling

Structured error handling with categorization:

```python
from uipath.runtime.error import (
    UiPathRuntimeError,
    UiPathErrorCode,
    UiPathErrorCategory
)

raise UiPathRuntimeError(
    UiPathErrorCode.EXECUTION_ERROR,
    "Failed to execute agent",
    "Detailed error message here",
    UiPathErrorCategory.USER,
)
```
