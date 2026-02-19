# UiPath Runtime Integration Guide

This document provides step-by-step instructions to generate a complete UiPath runtime integration for any Python agentic framework (e.g., Google ADK, Pydantic AI, CrewAI, AutoGen, etc.).

## Prerequisites

Before starting, you need:
1. The **target framework** name and its PyPI package (e.g., `google-adk`, `pydantic-ai`)
2. The **uipath-runtime** package (`uipath-runtime`) - this contains all protocols/contracts
3. The **uipath** SDK (`uipath`) - core UiPath functionality
4. Familiarity with the target framework's agent/workflow execution model

## Architecture Overview

A UiPath runtime integration is a Python package that bridges a third-party agentic framework with the UiPath platform. It implements a standardized protocol so UiPath can:
- **Discover** available agents/workflows from a config file
- **Infer schemas** (input/output JSON schemas) from user code
- **Execute** agents and return structured results
- **Stream** execution events in real-time
- **Suspend/Resume** execution for human-in-the-loop (HITL) scenarios
- **Visualize** agent graphs (nodes, edges, tools, handoffs)

### Protocol Hierarchy

```
UiPathRuntimeProtocol (main contract)
├── UiPathExecutableProtocol    → execute(input, options) -> UiPathRuntimeResult
├── UiPathStreamableProtocol    → stream(input, options) -> AsyncGenerator[UiPathRuntimeEvent]
├── UiPathSchemaProtocol        → get_schema() -> UiPathRuntimeSchema
└── UiPathDisposableProtocol    → dispose() -> None

UiPathRuntimeFactoryProtocol (factory contract)
├── discover_entrypoints() -> list[str]
├── new_runtime(entrypoint, runtime_id) -> UiPathRuntimeProtocol
├── get_storage() -> UiPathRuntimeStorageProtocol | None
├── get_settings() -> UiPathRuntimeFactorySettings | None
└── dispose() -> None
```

### Wrapper/Decorator Pattern

The factory wraps the base runtime with additional capabilities:

```
UiPathResumableRuntime          ← Adds HITL/resume trigger management
  └── YourFrameworkRuntime      ← Your base integration (implements UiPathRuntimeProtocol)
```

At a higher level, the platform may further wrap with:
```
UiPathExecutionRuntime          ← Adds tracing/telemetry
  └── UiPathChatRuntime         ← Adds conversational chat bridge
    └── UiPathDebugRuntime      ← Adds debugger/breakpoint support
      └── UiPathResumableRuntime
        └── YourFrameworkRuntime
```

You only implement `YourFrameworkRuntime` and `YourFrameworkFactory`. The wrappers are provided by `uipath-runtime`.

---

## Package Structure

Create a new package with this layout (replace `{framework}` with short name, e.g., `google_adk`, `pydantic_ai`):

```
uipath-{framework}/
├── pyproject.toml
├── README.md
├── src/
│   └── uipath_{framework}/
│       ├── __init__.py
│       ├── middlewares.py                  # Optional: UiPath SDK middlewares
│       └── runtime/
│           ├── __init__.py                 # Factory registration + exports
│           ├── factory.py                  # UiPathRuntimeFactoryProtocol impl
│           ├── runtime.py                  # UiPathRuntimeProtocol impl
│           ├── schema.py                   # Schema inference (input/output/graph)
│           ├── loader.py                   # Dynamic agent/workflow loading
│           ├── config.py                   # Config file parser ({framework}.json)
│           ├── errors.py                   # Framework-specific error codes
│           └── _storage.py                 # Optional: SQLite storage for HITL
├── tests/
│   ├── test_schema_inference.py
│   ├── test_context.py
│   ├── test_graph.py
│   ├── test_integration.py
│   └── test_storage.py                    # If HITL supported
└── samples/
    └── quickstart-agent/
        ├── pyproject.toml
        ├── {framework}.json
        └── main.py
```

---

## Step-by-Step Implementation

### STEP 1: Project Setup and Configuration

**Goal:** Create the package skeleton, config parser, and agent loader.

#### 1.1 Create `pyproject.toml`

```toml
[project]
name = "uipath-{framework}"
version = "0.0.1"
description = "UiPath {Framework} SDK"
readme = "README.md"
requires-python = ">=3.11"
dependencies = [
    "aiosqlite>=0.20.0",
    "{framework-pypi-package}>=X.Y.Z",          # The target framework
    "uipath>=2.8.0, <2.9.0",
    "uipath-runtime>=0.6.0, <0.7.0",
]

[project.entry-points."uipath.runtime.factories"]
{framework-id} = "uipath_{framework}.runtime:register_runtime_factory"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.pytest.ini_options]
testpaths = ["tests"]
python_files = "test_*.py"
addopts = "-ra -q"
asyncio_default_fixture_loop_scope = "function"
asyncio_mode = "auto"
```

**Key:** The `[project.entry-points."uipath.runtime.factories"]` section registers your factory so it's auto-discovered by the UiPath CLI.

#### 1.2 Create Configuration File Parser (`config.py`)

The config file (e.g., `{framework}.json`) maps entrypoint names to Python file paths:

```json
{
  "agents": {
    "my_agent": "main.py:agent",
    "my_workflow": "src/workflows/main.py:workflow"
  }
}
```

The format is always `"file_path:variable_name"`.

**Implementation pattern:**

```python
"""Configuration file parser for {Framework} runtime."""
import json
import os

CONFIG_FILE = "{framework}.json"  # e.g., "google_adk.json", "pydantic_ai.json"

class FrameworkConfig:
    def __init__(self, config_path: str = CONFIG_FILE):
        self._config_path = config_path
        self._agents: dict[str, str] | None = None

    @property
    def exists(self) -> bool:
        return os.path.isfile(self._config_path)

    @property
    def agents(self) -> dict[str, str]:
        if self._agents is None:
            self._agents = self._load_agents()
        return self._agents

    @property
    def entrypoints(self) -> list[str]:
        return list(self.agents.keys())

    def _load_agents(self) -> dict[str, str]:
        with open(self._config_path) as f:
            data = json.load(f)
        if "agents" not in data or not isinstance(data["agents"], dict):
            raise ValueError(f"Invalid {self._config_path}: missing 'agents' dict")
        return data["agents"]
```

#### 1.3 Create Agent/Workflow Loader (`loader.py`)

Dynamically imports the user's agent from a Python file.

**Implementation pattern:**

```python
"""Dynamic agent loader for {Framework}."""
import importlib.util
import inspect
import os
import sys
from contextlib import asynccontextmanager

class AgentLoader:
    def __init__(self, name: str, file_path: str, variable_name: str):
        self.name = name
        self.file_path = file_path
        self.variable_name = variable_name
        self._context_manager = None

    @classmethod
    def from_path_string(cls, name: str, path_string: str) -> "AgentLoader":
        """Parse 'file.py:variable' format."""
        if ":" not in path_string:
            raise ValueError(f"Invalid path format: {path_string}. Expected 'file.py:variable'")
        file_path, variable_name = path_string.split(":", 1)
        return cls(name, file_path, variable_name)

    async def load(self):
        """Load and return the agent/workflow object."""
        # 1. Security: ensure file is within CWD
        abs_path = os.path.abspath(os.path.normpath(self.file_path))
        cwd = os.path.abspath(os.getcwd())
        if not abs_path.startswith(cwd):
            raise ValueError(f"Agent file must be within current directory: {self.file_path}")

        if not os.path.isfile(abs_path):
            raise FileNotFoundError(f"Agent file not found: {abs_path}")

        # 2. Setup Python path
        if cwd not in sys.path:
            sys.path.insert(0, cwd)
        src_dir = os.path.join(cwd, "src")
        if os.path.isdir(src_dir) and src_dir not in sys.path:
            sys.path.insert(0, src_dir)

        # 3. Import module
        module_name = f"_uipath_agent_{self.name}"
        spec = importlib.util.spec_from_file_location(module_name, abs_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        # 4. Get variable
        agent_obj = getattr(module, self.variable_name, None)
        if agent_obj is None:
            raise AttributeError(f"Variable '{self.variable_name}' not found in {self.file_path}")

        # 5. Resolve if callable/async/context-manager
        return await self._resolve(agent_obj)

    async def _resolve(self, obj):
        """Resolve agent from various definition patterns."""
        # Direct instance - return as-is
        if not callable(obj) or isinstance(obj, type):
            return obj

        # Try calling it
        result = obj()

        # Async context manager
        if hasattr(result, "__aenter__"):
            self._context_manager = result
            return await result.__aenter__()

        # Awaitable (async function)
        if inspect.isawaitable(result):
            return await result

        return result

    async def cleanup(self):
        if self._context_manager:
            await self._context_manager.__aexit__(None, None, None)
            self._context_manager = None
```

#### 1.4 Create Error Definitions (`errors.py`)

```python
"""Framework-specific error codes."""
from enum import Enum
from uipath.runtime.errors import UiPathBaseRuntimeError, UiPathErrorCategory

class FrameworkErrorCode(str, Enum):
    AGENT_EXECUTION_FAILURE = "AGENT_EXECUTION_FAILURE"
    TIMEOUT_ERROR = "TIMEOUT_ERROR"
    SERIALIZE_OUTPUT_ERROR = "SERIALIZE_OUTPUT_ERROR"
    CONFIG_MISSING = "CONFIG_MISSING"
    CONFIG_INVALID = "CONFIG_INVALID"
    AGENT_NOT_FOUND = "AGENT_NOT_FOUND"
    AGENT_TYPE_ERROR = "AGENT_TYPE_ERROR"
    AGENT_LOAD_ERROR = "AGENT_LOAD_ERROR"
    AGENT_IMPORT_ERROR = "AGENT_IMPORT_ERROR"

class FrameworkRuntimeError(UiPathBaseRuntimeError):
    def __init__(self, code, title, detail, category=UiPathErrorCategory.UNKNOWN, status=None):
        super().__init__(
            code=code, title=title, detail=detail,
            category=category, status=status, prefix="{Framework}"
        )
```

#### Validation for Step 1

Write tests to verify:
- Config file parsing (valid JSON, missing fields, non-existent file)
- Agent loader path validation (security check, missing file, missing variable)
- Agent loader resolution (direct instance, sync function, async function)
- Error code mapping

```python
# tests/test_config.py
import pytest, json, os

def test_config_loads_valid_file(tmp_path):
    config_file = tmp_path / "{framework}.json"
    config_file.write_text(json.dumps({"agents": {"agent1": "main.py:agent"}}))
    os.chdir(tmp_path)
    config = FrameworkConfig(str(config_file))
    assert config.exists
    assert config.entrypoints == ["agent1"]

def test_config_rejects_invalid_format(tmp_path):
    config_file = tmp_path / "{framework}.json"
    config_file.write_text(json.dumps({"wrong_key": {}}))
    with pytest.raises(ValueError):
        FrameworkConfig(str(config_file)).agents

def test_loader_rejects_path_outside_cwd():
    loader = AgentLoader("test", "/etc/passwd", "agent")
    with pytest.raises(ValueError, match="within current directory"):
        await loader.load()
```

---

### STEP 2: Schema Inference (`get_schema()`)

**Goal:** Automatically detect input/output JSON schemas from the user's agent code.

This is the most framework-specific step. You must understand how the target framework defines:
- **Input**: What data does the agent accept? (messages, structured config, context objects)
- **Output**: What does the agent return? (text, structured data, Pydantic models)
- **Graph**: What is the agent's structure? (nodes, tools, handoffs, subgraphs)

#### 2.1 Research the Framework's Input/Output Model

For each framework, identify:

| Framework | Input Model | Output Model | Graph Model |
|-----------|------------|--------------|-------------|
| **OpenAI Agents** | `messages: str\|list` + `Agent[Context]` generic param | `agent.output_type` (Pydantic model or string) | Agents + handoffs + tools (flat) |
| **LlamaIndex** | `StartEvent` Pydantic model | `StopEvent` or `workflow.output_cls` | Workflow steps + event flow |
| **LangGraph** | `graph.get_input_jsonschema()` | `graph.get_output_jsonschema()` | `graph.get_graph(xray=N)` |
| **Google ADK** | `LlmAgent.input_schema` (Pydantic `BaseModel`) — only on `LlmAgent`, not `BaseAgent` | `LlmAgent.output_schema` (Pydantic `BaseModel`) or `LlmAgent.output_key` (str) | `BaseAgent.sub_agents` tree + `LlmAgent.tools` list (`BaseTool`, `Callable`, `AgentTool`) |
| **Pydantic AI** | `agent.run()` params / `deps_type` | `agent.result_type` | Agent + tools |
| **CrewAI** | Crew's input variables | Crew's output type | Agents + tasks graph |

#### 2.2 Schema Inference Principles

**The user's code dictates the schemas.** If the framework agent has strongly-typed input/output (e.g., Pydantic models), those types ARE the schemas. Messages is only the fallback for conversational agents without typed I/O.

**Key rules:**
1. **Use `isinstance` checks, not `getattr` guessing.** Verify the agent's actual type before accessing framework-specific attributes. Different agent types (e.g., LlmAgent vs composite agents) expose different attributes.
2. **Import types from their actual module paths**, not from `__init__.py` re-exports that may not be stable. E.g., `from framework.tools.base_tool import BaseTool` instead of `from framework.tools import BaseTool`.
3. **Use `uipath.runtime.schema` helpers** (`transform_references`, `transform_nullable_types`) instead of reimplementing ref resolution or nullable handling.
4. **Typed input replaces messages.** If the agent defines `input_schema` (Pydantic model), use that as the full input schema. Don't merge it with messages.
5. **Typed output replaces the generic result.** If the agent defines `output_schema` or equivalent, use that as the full output schema.

#### 2.3 Implement Schema Extraction (`schema.py`)

```python
"""Schema inference for {Framework} agents."""
from typing import Any
from pydantic import BaseModel, TypeAdapter
from uipath.runtime.schema import (
    UiPathRuntimeSchema, UiPathRuntimeGraph, UiPathRuntimeNode, UiPathRuntimeEdge,
    transform_references, transform_nullable_types,
)

# Import the actual agent types for isinstance checks
from framework import BaseAgent, LlmAgent  # Use real framework types


def get_entrypoints_schema(agent: BaseAgent) -> dict[str, Any]:
    """Extract input/output JSON schemas from a framework agent.

    The user's code defines the agent type and its I/O schemas.
    Use isinstance checks to determine what attributes are available.
    """
    schema: dict[str, Any] = {
        "input": _default_input_schema(),
        "output": _default_output_schema(),
    }

    # Only access typed I/O attributes on agent types that have them
    if not isinstance(agent, LlmAgent):
        return schema

    # Input: if agent has typed input_schema, use it as the full input
    input_type = agent.input_schema  # Framework-specific attribute
    if input_type is not None and _is_pydantic_model(input_type):
        try:
            adapter = TypeAdapter(input_type)
            json_schema = adapter.json_schema()
            resolved, _ = transform_references(json_schema)
            schema["input"] = {
                "type": "object",
                "properties": transform_nullable_types(
                    resolved.get("properties", {})
                ),
                "required": resolved.get("required", []),
            }
        except Exception:
            pass

    # Output: if agent has typed output, use it as the full output
    output_type = agent.output_schema  # Framework-specific attribute
    if output_type is not None and _is_pydantic_model(output_type):
        try:
            adapter = TypeAdapter(output_type)
            json_schema = adapter.json_schema()
            resolved, _ = transform_references(json_schema)
            schema["output"] = {
                "type": "object",
                "properties": transform_nullable_types(
                    resolved.get("properties", {})
                ),
                "required": resolved.get("required", []),
            }
        except Exception:
            pass

    return schema


def _default_input_schema() -> dict[str, Any]:
    """Fallback input schema for conversational agents without typed input."""
    return {
        "type": "object",
        "properties": {
            "messages": {
                "anyOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "object"}}
                ],
                "title": "Messages",
                "description": "User messages to send to the agent",
            }
        },
        "required": ["messages"],
    }


def _default_output_schema() -> dict[str, Any]:
    """Fallback output schema when no typed output is defined."""
    return {
        "type": "object",
        "properties": {
            "result": {
                "title": "Result",
                "description": "The agent's response",
                "anyOf": [
                    {"type": "string"},
                    {"type": "object"},
                    {"type": "array", "items": {"type": "object"}},
                ],
            }
        },
        "required": ["result"],
    }


def _is_pydantic_model(type_hint: Any) -> bool:
    """Check if a type hint is a Pydantic BaseModel class."""
    import inspect
    try:
        return inspect.isclass(type_hint) and issubclass(type_hint, BaseModel)
    except TypeError:
        return False
```

#### 2.3 Implement Graph Building

```python
def get_agent_graph(agent) -> UiPathRuntimeGraph:
    """Build a visualization graph from the agent's structure.

    Node types:
    - "__start__": Entry point
    - "__end__": Exit point
    - "node": Regular processing step
    - "model": LLM call step
    - "tool": Tool execution step
    - "external": Human interaction point (HITL)

    Edge rules:
    - __start__ -> first node (label: "input")
    - last node -> __end__ (label: "output")
    - Bidirectional edges for tool calls (agent <-> tools)
    - Bidirectional edges for handoffs (agent <-> agent)
    """
    nodes = []
    edges = []
    visited = set()  # Prevent circular references

    # Always add start/end
    nodes.append(UiPathRuntimeNode(id="__start__", name="__start__", type="__start__"))
    nodes.append(UiPathRuntimeNode(id="__end__", name="__end__", type="__end__"))

    # Framework-specific: traverse agent structure
    _add_agent_nodes(agent, nodes, edges, visited)

    # Connect start -> main agent
    if len(nodes) > 2:
        main_node = nodes[2]  # First non-control node
        edges.append(UiPathRuntimeEdge(source="__start__", target=main_node.id, label="input"))
        edges.append(UiPathRuntimeEdge(source=main_node.id, target="__end__", label="output"))

    return UiPathRuntimeGraph(nodes=nodes, edges=edges)


def _add_agent_nodes(agent, nodes, edges, visited):
    """Recursively add agent nodes. FRAMEWORK-SPECIFIC.

    For each framework, detect:
    1. Sub-agents / handoffs -> create nodes + bidirectional edges
    2. Tools -> create aggregated tool nodes with metadata
    3. Subgraphs -> create nodes with nested UiPathRuntimeGraph
    """
    raise NotImplementedError("Implement for your framework")
```

#### Validation for Step 2

```python
# tests/test_schema_inference.py
import pytest

def test_conversational_agent_uses_messages():
    """Agent without typed input falls back to messages."""
    agent = create_basic_agent()  # No input_schema
    schema = get_entrypoints_schema(agent)
    assert "messages" in schema["input"]["properties"]
    assert "messages" in schema["input"]["required"]

def test_typed_input_replaces_messages():
    """Agent with input_schema uses it as the full input — no messages."""
    agent = create_agent_with_input_schema()
    schema = get_entrypoints_schema(agent)
    assert "my_field" in schema["input"]["properties"]
    assert "messages" not in schema["input"]["properties"]

def test_typed_output_replaces_generic_result():
    """Agent with output_schema uses it as the full output."""
    agent = create_agent_with_output_schema()
    schema = get_entrypoints_schema(agent)
    assert "my_result" in schema["output"]["properties"]
    assert "result" not in schema["output"]["properties"]

def test_output_schema_fallback():
    """Agent without typed output falls back to generic result."""
    agent = create_basic_agent()
    schema = get_entrypoints_schema(agent)
    assert "result" in schema["output"]["properties"]

def test_composite_agent_gets_default_schemas():
    """Composite/orchestrator agents that don't have typed I/O get defaults."""
    agent = create_composite_agent()
    schema = get_entrypoints_schema(agent)
    assert "messages" in schema["input"]["properties"]
    assert "result" in schema["output"]["properties"]

def test_graph_has_start_end():
    agent = create_test_agent()
    graph = get_agent_graph(agent)
    node_ids = {n.id for n in graph.nodes}
    assert "__start__" in node_ids
    assert "__end__" in node_ids

def test_graph_tools_have_metadata():
    agent = create_agent_with_tools()
    graph = get_agent_graph(agent)
    tool_nodes = [n for n in graph.nodes if n.type == "tool"]
    assert len(tool_nodes) > 0
    assert "tool_names" in tool_nodes[0].metadata
```

---

### STEP 3: Execute (`execute()`)

**Goal:** Run the agent synchronously and return a structured result.

#### 3.1 Implement the Runtime Class (`runtime.py`)

```python
"""Runtime implementation for {Framework}."""
import uuid
from typing import Any, AsyncGenerator

from uipath.runtime import (
    UiPathExecuteOptions, UiPathStreamOptions,
    UiPathRuntimeResult, UiPathRuntimeStatus,
    UiPathRuntimeEvent, UiPathRuntimeSchema,
)
from uipath.runtime.events import UiPathRuntimeMessageEvent, UiPathRuntimeStateEvent

from uipath.core.serialization import serialize_defaults
from .schema import get_entrypoints_schema, get_agent_graph
from .errors import FrameworkRuntimeError, FrameworkErrorCode


class UiPathFrameworkRuntime:
    """UiPath runtime for {Framework} agents.

    Implements UiPathRuntimeProtocol via duck typing (Python Protocol).
    """

    def __init__(self, agent, entrypoint: str = "", runtime_id: str | None = None):
        self.agent = agent
        self.entrypoint = entrypoint
        self.runtime_id = runtime_id or str(uuid.uuid4())

    # ── execute ──────────────────────────────────────────────────────

    async def execute(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathExecuteOptions | None = None,
    ) -> UiPathRuntimeResult:
        """Execute the agent and return the final result."""
        try:
            # 1. Parse input into framework-specific format
            agent_input = self._prepare_input(input or {})

            # 2. Run the agent using the framework's runner
            #    FRAMEWORK-SPECIFIC: Replace with actual runner call
            #    Examples:
            #    - OpenAI Agents: Runner.run(starting_agent=self.agent, input=agent_input)
            #    - LlamaIndex:   workflow.run(**agent_input)
            #    - LangGraph:    graph.ainvoke(agent_input, config=self._get_config())
            #    - Pydantic AI:  agent.run(agent_input)
            #    - Google ADK:   runner.run(agent=self.agent, input=agent_input)
            result = await self._run_agent(agent_input, options)

            # 3. Serialize output to dict
            output = serialize_defaults(result)
            if not isinstance(output, dict):
                output = {"result": output}

            return UiPathRuntimeResult(
                output=output,
                status=UiPathRuntimeStatus.SUCCESSFUL,
            )

        except Exception as e:
            raise self._create_error(e) from e

    # ── stream (placeholder - implemented in Step 4) ─────────────────

    async def stream(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathStreamOptions | None = None,
    ) -> AsyncGenerator[UiPathRuntimeEvent, None]:
        """Stream execution events. Implemented in Step 4."""
        # Fallback: just execute and yield result
        result = await self.execute(input, options)
        yield result

    # ── get_schema ───────────────────────────────────────────────────

    async def get_schema(self) -> UiPathRuntimeSchema:
        """Return input/output schema and graph structure."""
        schemas = get_entrypoints_schema(self.agent)
        graph = get_agent_graph(self.agent)

        return UiPathRuntimeSchema(
            filePath=self.entrypoint,
            uniqueId=str(uuid.uuid4()),
            type="agent",
            input=schemas["input"],
            output=schemas["output"],
            graph=graph,
        )

    # ── dispose ──────────────────────────────────────────────────────

    async def dispose(self) -> None:
        """Clean up resources."""
        pass

    # ── Internal helpers ─────────────────────────────────────────────

    def _prepare_input(self, input: dict[str, Any]) -> Any:
        """Convert UiPath input dict to framework-specific format.

        FRAMEWORK-SPECIFIC. Common patterns:
        - Extract "messages" field for conversational input
        - Parse remaining fields into context/deps object
        - Map UiPath message format to framework's message types
        """
        messages = input.get("messages", "")
        # Return framework-specific input format
        return messages

    async def _run_agent(self, agent_input, options):
        """Run the agent using the framework's execution API.

        FRAMEWORK-SPECIFIC. This is the core integration point.
        """
        raise NotImplementedError("Implement with framework's runner")

    def _create_error(self, e: Exception) -> FrameworkRuntimeError:
        """Map exceptions to structured runtime errors."""
        import json
        if isinstance(e, json.JSONDecodeError):
            return FrameworkRuntimeError(
                code=FrameworkErrorCode.AGENT_EXECUTION_FAILURE,
                title="Invalid JSON input",
                detail=str(e),
            )
        if isinstance(e, TimeoutError):
            return FrameworkRuntimeError(
                code=FrameworkErrorCode.TIMEOUT_ERROR,
                title="Agent execution timed out",
                detail=str(e),
            )
        return FrameworkRuntimeError(
            code=FrameworkErrorCode.AGENT_EXECUTION_FAILURE,
            title="Agent execution failed",
            detail=str(e),
        )
```

#### 3.2 Output Serialization

Use the `serialize_defaults` function from `uipath.core.serialization` — do NOT create a custom `_serialize.py`. This utility handles all common Python types (Pydantic models, dataclasses, dicts, lists, enums, primitives) and is already tested and maintained by the core SDK.

```python
from uipath.core.serialization import serialize_defaults

# In execute():
output = serialize_defaults(result)
if not isinstance(output, dict):
    output = {"result": output}

# In stream() for event payloads:
payload = serialize_defaults(event_data)
```

#### Validation for Step 3

```python
# tests/test_execute.py
import pytest

@pytest.mark.asyncio
async def test_execute_returns_successful_result():
    runtime = create_test_runtime()  # With a mock agent
    result = await runtime.execute({"messages": "Hello"})
    assert result.status == UiPathRuntimeStatus.SUCCESSFUL
    assert isinstance(result.output, dict)

@pytest.mark.asyncio
async def test_execute_wraps_errors():
    runtime = create_failing_runtime()
    with pytest.raises(FrameworkRuntimeError):
        await runtime.execute({"messages": "trigger error"})

@pytest.mark.asyncio
async def test_get_schema_returns_valid_schema():
    runtime = create_test_runtime()
    schema = await runtime.get_schema()
    assert schema.type == "agent"
    assert "messages" in schema.input["properties"]
    assert schema.graph is not None
```

---

### STEP 4: Streaming (`stream()`)

**Goal:** Yield real-time execution events during agent execution.

#### 4.1 Understand Event Types

Your `stream()` must yield these event types:

| Event | When to emit | Payload |
|-------|-------------|---------|
| `UiPathRuntimeMessageEvent` | AI generates text, tool calls, reasoning | Framework-specific message object (serialized to dict) |
| `UiPathRuntimeStateEvent` | Agent state changes, node transitions | `{"key": "value"}` state dict, with `node_name` |
| `UiPathRuntimeResult` | **Final event** - execution complete | Output dict + status |

#### 4.2 Implement Streaming

Replace the placeholder `stream()` in runtime.py:

```python
async def stream(
    self,
    input: dict[str, Any] | None = None,
    options: UiPathStreamOptions | None = None,
) -> AsyncGenerator[UiPathRuntimeEvent, None]:
    """Stream execution events in real-time."""
    try:
        agent_input = self._prepare_input(input or {})

        # FRAMEWORK-SPECIFIC: Use the framework's streaming API
        # Examples:
        #   OpenAI: Runner.run_streamed() + result.stream_events()
        #   LlamaIndex: handler.stream_events(expose_internal=True)
        #   LangGraph: graph.astream() with stream_mode=["messages","updates","tasks"]
        #   Pydantic AI: agent.run_stream()
        #   Google ADK: runner.run_stream()

        async for event in self._stream_agent(agent_input, options):
            runtime_event = self._convert_event(event)
            if runtime_event is not None:
                yield runtime_event

        # Final result (MUST be last yield)
        result = self._get_final_result()
        yield UiPathRuntimeResult(
            output=serialize_defaults(result),
            status=UiPathRuntimeStatus.SUCCESSFUL,
        )

    except Exception as e:
        raise self._create_error(e) from e


def _convert_event(self, event) -> UiPathRuntimeEvent | None:
    """Convert a framework event to a UiPath runtime event.

    FRAMEWORK-SPECIFIC. Map each framework event type:

    For MESSAGE events (AI text, tool calls, reasoning):
        return UiPathRuntimeMessageEvent(
            payload=serialize_defaults(event),
            metadata={"event_name": "message_created"},
        )

    For STATE events (node transitions, state updates):
        return UiPathRuntimeStateEvent(
            payload=serialize_defaults(state_dict),
            node_name="node_that_produced_this",
            metadata={"event_name": "state_update"},
        )

    For events to SKIP (raw HTTP responses, internal bookkeeping):
        return None
    """
    raise NotImplementedError("Implement event conversion for your framework")
```

#### 4.3 Event Mapping Reference

**OpenAI Agents events:**
- `run_item_stream_event` + `message_output_created` → `UiPathRuntimeMessageEvent`
- `run_item_stream_event` + `reasoning_item_created` → `UiPathRuntimeMessageEvent`
- `run_item_stream_event` (other) → `UiPathRuntimeStateEvent`
- `agent_updated_stream_event` → `UiPathRuntimeStateEvent`
- Raw response events → skip (too granular)

**LlamaIndex events:**
- `AgentInput`, `AgentOutput`, `AgentStream` → `UiPathRuntimeMessageEvent`
- `ToolCall`, `ToolCallResult` → `UiPathRuntimeMessageEvent`
- Other workflow events → `UiPathRuntimeStateEvent`

**LangGraph stream chunks:**
- `messages` tuple (BaseMessage/AIMessageChunk) → `UiPathRuntimeMessageEvent`
- `updates` tuple (node state dicts) → `UiPathRuntimeStateEvent`
- `tasks` tuple → `UiPathRuntimeStateEvent`

#### Validation for Step 4

```python
# tests/test_streaming.py
import pytest

@pytest.mark.asyncio
async def test_stream_yields_events():
    runtime = create_test_runtime()
    events = []
    async for event in runtime.stream({"messages": "Hello"}):
        events.append(event)
    assert len(events) >= 1
    assert isinstance(events[-1], UiPathRuntimeResult)  # Last event is result

@pytest.mark.asyncio
async def test_stream_message_events():
    runtime = create_test_runtime()
    message_events = []
    async for event in runtime.stream({"messages": "Hello"}):
        if isinstance(event, UiPathRuntimeMessageEvent):
            message_events.append(event)
    assert len(message_events) > 0
    assert all(e.payload is not None for e in message_events)
```

---

### STEP 5: Human-in-the-Loop / Durable Execution

**Goal:** Support suspending execution when human input is needed, and resuming later.

Not all frameworks support this. Check if the target framework has:
- **Interrupt/suspend semantics** (LangGraph `interrupt()`, LlamaIndex `InputRequiredEvent`)
- **State serialization** (checkpointing, context saving)
- **Resume capability** (continuing from a saved state)

#### 5.1 Determine HITL Capability

| Framework | HITL Support | Mechanism |
|-----------|-------------|-----------|
| **LangGraph** | Full | `interrupt()` function, `Command(resume=...)`, `AsyncSqliteSaver` checkpointer |
| **LlamaIndex** | Full | `InputRequiredEvent`, `HumanResponseEvent`, `JsonPickleSerializer` context save |
| **OpenAI Agents** | None built-in | No native suspend/resume (would need custom implementation) |
| **Google ADK** | Partial | Depends on orchestration mode |
| **Pydantic AI** | None built-in | No native suspend/resume |
| **CrewAI** | Partial | Human input tool, but no state serialization |

#### 5.2 Implement Storage (`_storage.py`)

If the framework supports HITL, implement SQLite-based storage:

```python
"""SQLite storage for resume triggers and state persistence."""
import json
import asyncio
import aiosqlite
from typing import Any
from uipath.runtime import UiPathResumeTrigger

class SqliteResumableStorage:
    """Async SQLite storage for HITL resume triggers and KV data."""

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def initialize(self):
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")

        # Resume triggers table
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS __uipath_resume_triggers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                runtime_id TEXT NOT NULL,
                interrupt_id TEXT,
                data TEXT NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_triggers_runtime ON __uipath_resume_triggers(runtime_id)"
        )

        # Key-value table
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS __uipath_runtime_kv (
                runtime_id TEXT NOT NULL,
                namespace TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (runtime_id, namespace, key)
            )
        """)
        await self._db.commit()

    # UiPathResumableStorageProtocol methods
    async def save_triggers(self, runtime_id: str, triggers: list[UiPathResumeTrigger]):
        async with self._lock:
            await self._db.execute(
                "DELETE FROM __uipath_resume_triggers WHERE runtime_id = ?", (runtime_id,)
            )
            for trigger in triggers:
                await self._db.execute(
                    "INSERT INTO __uipath_resume_triggers (runtime_id, interrupt_id, data) VALUES (?, ?, ?)",
                    (runtime_id, trigger.interrupt_id, trigger.model_dump_json(by_alias=True)),
                )
            await self._db.commit()

    async def get_triggers(self, runtime_id: str) -> list[UiPathResumeTrigger] | None:
        async with self._lock:
            cursor = await self._db.execute(
                "SELECT data FROM __uipath_resume_triggers WHERE runtime_id = ?", (runtime_id,)
            )
            rows = await cursor.fetchall()
            if not rows:
                return None
            return [UiPathResumeTrigger.model_validate_json(row[0]) for row in rows]

    async def delete_trigger(self, runtime_id: str, trigger: UiPathResumeTrigger):
        async with self._lock:
            await self._db.execute(
                "DELETE FROM __uipath_resume_triggers WHERE runtime_id = ? AND interrupt_id = ?",
                (runtime_id, trigger.interrupt_id),
            )
            await self._db.commit()

    # UiPathRuntimeStorageProtocol methods
    async def set_value(self, runtime_id: str, namespace: str, key: str, value: Any):
        async with self._lock:
            if value is None:
                serialized = None
            elif isinstance(value, str):
                serialized = f"s:{value}"
            else:
                serialized = f"j:{json.dumps(value)}"
            await self._db.execute(
                "INSERT OR REPLACE INTO __uipath_runtime_kv VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
                (runtime_id, namespace, key, serialized),
            )
            await self._db.commit()

    async def get_value(self, runtime_id: str, namespace: str, key: str) -> Any:
        async with self._lock:
            cursor = await self._db.execute(
                "SELECT value FROM __uipath_runtime_kv WHERE runtime_id=? AND namespace=? AND key=?",
                (runtime_id, namespace, key),
            )
            row = await cursor.fetchone()
            if row is None or row[0] is None:
                return None
            val = row[0]
            if val.startswith("s:"):
                return val[2:]
            if val.startswith("j:"):
                return json.loads(val[2:])
            return val

    async def dispose(self):
        if self._db:
            await self._db.close()
            self._db = None
```

#### 5.3 Implement Suspend/Resume in Runtime

When the framework signals a need for human input:

```python
# In runtime.py - extend execute() and stream()

async def _handle_suspension(self, suspend_value, runtime_id):
    """Handle agent suspension for HITL.

    Returns UiPathRuntimeResult with SUSPENDED status and resume trigger info.

    FRAMEWORK-SPECIFIC:
    - LangGraph: detect state.interrupts, save checkpointed state
    - LlamaIndex: detect InputRequiredEvent, save context via JsonPickleSerializer
    """
    return UiPathRuntimeResult(
        output=serialize_defaults(suspend_value),
        status=UiPathRuntimeStatus.SUSPENDED,
        # Triggers are managed by UiPathResumableRuntime wrapper
    )

async def _handle_resume(self, input, options):
    """Resume a suspended execution.

    FRAMEWORK-SPECIFIC:
    - LangGraph: wrap input in Command(resume=input), graph resumes from checkpoint
    - LlamaIndex: load saved context, send HumanResponseEvent
    """
    raise NotImplementedError("Implement resume for your framework")
```

#### 5.4 Wire HITL into Factory

```python
# In factory.py - new_runtime() wraps with UiPathResumableRuntime

from uipath.runtime import UiPathResumableRuntime

async def new_runtime(self, entrypoint, runtime_id, **kwargs):
    agent = await self._resolve_agent(entrypoint)
    base_runtime = UiPathFrameworkRuntime(agent, entrypoint, runtime_id)

    # Only wrap if framework supports HITL
    if self._supports_hitl():
        storage = await self.get_storage()
        trigger_manager = self._create_trigger_manager()
        return UiPathResumableRuntime(
            delegate=base_runtime,
            storage=storage,
            trigger_manager=trigger_manager,
            runtime_id=runtime_id,
        )
    return base_runtime
```

#### Validation for Step 5

```python
# tests/test_storage.py
import pytest

@pytest.mark.asyncio
async def test_save_and_retrieve_triggers(tmp_path):
    storage = SqliteResumableStorage(str(tmp_path / "state.db"))
    await storage.initialize()

    trigger = UiPathResumeTrigger(interrupt_id="int-1", ...)
    await storage.save_triggers("runtime-1", [trigger])
    retrieved = await storage.get_triggers("runtime-1")
    assert len(retrieved) == 1
    assert retrieved[0].interrupt_id == "int-1"

    await storage.dispose()

@pytest.mark.asyncio
async def test_kv_store(tmp_path):
    storage = SqliteResumableStorage(str(tmp_path / "state.db"))
    await storage.initialize()

    await storage.set_value("rt-1", "ns", "key", {"foo": "bar"})
    val = await storage.get_value("rt-1", "ns", "key")
    assert val == {"foo": "bar"}

    await storage.dispose()

# tests/test_hitl.py - if framework supports it
@pytest.mark.asyncio
async def test_suspend_and_resume():
    runtime = create_hitl_runtime()
    result = await runtime.execute({"messages": "request approval"})
    assert result.status == UiPathRuntimeStatus.SUSPENDED

    resumed = await runtime.execute(
        {"messages": "approved"},
        options=UiPathExecuteOptions(resume=True),
    )
    assert resumed.status == UiPathRuntimeStatus.SUCCESSFUL
```

---

### STEP 6: Chat Message Streaming (UiPathConversation Format)

**Goal:** Convert framework-specific streaming messages to UiPath's conversation protocol for real-time chat UIs.

This step is needed when the runtime is used in a **conversational context** (chat UI). The `UiPathChatRuntime` wrapper handles the chat protocol, but your runtime's `UiPathRuntimeMessageEvent` payloads should be structured enough for conversion.

#### 6.1 UiPath Conversation Event Hierarchy

```
UiPathConversationEvent
├── conversation_id
└── exchange (one turn)
    ├── exchange_id
    └── message
        ├── message_id
        ├── start (role, timestamp)
        ├── content_part
        │   ├── start (content_part_id, mime_type)
        │   ├── chunk (data: text chunk)
        │   └── end
        ├── tool_call
        │   ├── start (tool_call_id, name, input)
        │   └── end (output, timestamp)
        └── end
```

#### 6.2 Message Mapper (Optional but recommended for chat-heavy frameworks)

If your framework has rich message types (like LangChain's `BaseMessage`, `AIMessageChunk`), implement a mapper:

```python
"""Chat message mapper for {Framework} <-> UiPath conversation format."""
from uipath.core.chat import (
    UiPathConversationMessageEvent,
    UiPathConversationMessageStartEvent,
    UiPathConversationMessageEndEvent,
    UiPathConversationContentPartEvent,
    UiPathConversationContentPartStartEvent,
    UiPathConversationContentPartChunkEvent,
    UiPathConversationContentPartEndEvent,
    UiPathConversationToolCallEvent,
    UiPathConversationToolCallStartEvent,
    UiPathConversationToolCallEndEvent,
)

class ChatMessageMapper:
    """Maps framework messages to UiPath conversation events."""

    def __init__(self, storage=None):
        self._current_message_id: str | None = None
        self._storage = storage  # For correlating tool calls to messages

    def map_event(self, message) -> list[UiPathConversationMessageEvent]:
        """Convert a framework message to UiPath conversation events.

        FRAMEWORK-SPECIFIC. Must handle:
        1. Message start (new AI message) -> MessageStartEvent
        2. Text chunks -> ContentPartChunkEvent
        3. Tool call starts -> ToolCallStartEvent
        4. Tool results -> ToolCallEndEvent
        5. Message end -> MessageEndEvent
        """
        raise NotImplementedError("Implement for your framework")

    def map_input_messages(self, messages) -> list:
        """Convert UiPath message format to framework's message types.

        FRAMEWORK-SPECIFIC. Handle:
        - String messages -> framework's user message type
        - UiPathConversationMessage list -> framework message types
        - Dict list -> parse to framework message types
        """
        raise NotImplementedError("Implement for your framework")
```

#### Validation for Step 6

```python
# tests/test_messages.py
import pytest

def test_map_ai_message_to_conversation_events():
    mapper = ChatMessageMapper()
    events = mapper.map_event(create_ai_message("Hello!"))
    assert any(e.start is not None for e in events)  # Has start event
    assert any(e.content_part is not None for e in events)  # Has content

def test_map_tool_call_events():
    mapper = ChatMessageMapper()
    events = mapper.map_event(create_tool_call_message())
    tool_events = [e for e in events if e.tool_call is not None]
    assert len(tool_events) > 0
```

---

### STEP 7: Factory Implementation and Registration

**Goal:** Tie everything together with the factory that creates runtime instances.

#### 7.1 Implement the Factory (`factory.py`)

```python
"""Runtime factory for {Framework}."""
import asyncio
import logging
from typing import Any

from uipath.runtime import (
    UiPathRuntimeContext,
    UiPathRuntimeFactoryProtocol,
    UiPathRuntimeFactorySettings,
    UiPathRuntimeProtocol,
    UiPathRuntimeStorageProtocol,
    UiPathResumableRuntime,
)

from .config import FrameworkConfig, CONFIG_FILE
from .loader import AgentLoader
from .runtime import UiPathFrameworkRuntime
from .errors import FrameworkRuntimeError, FrameworkErrorCode
from ._storage import SqliteResumableStorage

logger = logging.getLogger(__name__)


class UiPathFrameworkRuntimeFactory:
    """Factory for creating {Framework} runtime instances.

    Implements UiPathRuntimeFactoryProtocol via duck typing.
    """

    def __init__(self, context: UiPathRuntimeContext | None = None):
        self.context = context or UiPathRuntimeContext()
        self._agent_cache: dict[str, Any] = {}
        self._agent_loaders: dict[str, AgentLoader] = {}
        self._agent_lock = asyncio.Lock()
        self._storage: SqliteResumableStorage | None = None

        # Optional: instrument framework for tracing
        # e.g., OpenAIAgentsInstrumentor().instrument()

    def discover_entrypoints(self) -> list[str]:
        """Return list of available agent names from config."""
        config = FrameworkConfig()
        if not config.exists:
            return []
        return config.entrypoints

    async def new_runtime(
        self, entrypoint: str, runtime_id: str, **kwargs
    ) -> UiPathRuntimeProtocol:
        """Create a new runtime instance for an entrypoint."""
        agent = await self._resolve_agent(entrypoint)
        storage = await self.get_storage()

        base_runtime = UiPathFrameworkRuntime(
            agent=agent,
            entrypoint=entrypoint,
            runtime_id=runtime_id,
            storage=storage,  # Pass storage if runtime needs it (e.g., for state)
        )

        # Wrap with UiPathResumableRuntime for HITL support (if applicable)
        # Remove this wrapping if framework doesn't support HITL
        return base_runtime

    async def get_storage(self) -> UiPathRuntimeStorageProtocol | None:
        """Get or create shared SQLite storage."""
        if self._storage is None:
            db_path = self.context.state_file_path or "__uipath/state.db"
            self._storage = SqliteResumableStorage(db_path)
            await self._storage.initialize()
        return self._storage

    async def get_settings(self) -> UiPathRuntimeFactorySettings | None:
        return None

    async def dispose(self) -> None:
        """Clean up all resources."""
        for loader in self._agent_loaders.values():
            try:
                await loader.cleanup()
            except Exception as e:
                logger.warning(f"Error cleaning up loader: {e}")
        self._agent_cache.clear()
        self._agent_loaders.clear()
        if self._storage:
            await self._storage.dispose()
            self._storage = None

    async def _resolve_agent(self, entrypoint: str):
        """Load and cache agent for an entrypoint."""
        async with self._agent_lock:
            if entrypoint in self._agent_cache:
                return self._agent_cache[entrypoint]

            config = FrameworkConfig()
            if not config.exists:
                raise FrameworkRuntimeError(
                    code=FrameworkErrorCode.CONFIG_MISSING,
                    title="Config file not found",
                    detail=f"{CONFIG_FILE} not found in current directory",
                )

            if entrypoint not in config.agents:
                raise FrameworkRuntimeError(
                    code=FrameworkErrorCode.AGENT_NOT_FOUND,
                    title="Agent not found",
                    detail=f"Agent '{entrypoint}' not in {CONFIG_FILE}",
                )

            path_string = config.agents[entrypoint]
            loader = AgentLoader.from_path_string(entrypoint, path_string)
            agent = await loader.load()

            self._agent_cache[entrypoint] = agent
            self._agent_loaders[entrypoint] = loader
            return agent
```

#### 7.2 Register the Factory (`__init__.py`)

```python
"""UiPath {Framework} Runtime Package."""
from uipath.runtime import UiPathRuntimeFactoryRegistry, UiPathRuntimeContext

from .factory import UiPathFrameworkRuntimeFactory
from .config import CONFIG_FILE


def register_runtime_factory():
    """Register the {Framework} runtime factory with UiPath."""
    def create_factory(context: UiPathRuntimeContext | None = None):
        return UiPathFrameworkRuntimeFactory(
            context=context if context else UiPathRuntimeContext()
        )

    UiPathRuntimeFactoryRegistry.register(
        "{framework-id}",    # e.g., "google-adk", "pydantic-ai"
        create_factory,
        CONFIG_FILE,         # e.g., "google_adk.json"
    )
```

#### 7.3 Entry Point in `pyproject.toml`

Already covered in Step 1, but critical:

```toml
[project.entry-points."uipath.runtime.factories"]
{framework-id} = "uipath_{framework}.runtime:register_runtime_factory"
```

This allows `uipath run` CLI to auto-discover your factory when the config file is present.

#### Validation for Step 7

```python
# tests/test_factory.py
import pytest

@pytest.mark.asyncio
async def test_factory_discovers_entrypoints(tmp_path):
    # Setup config file
    write_config(tmp_path, {"agents": {"agent1": "main.py:agent"}})
    os.chdir(tmp_path)

    factory = UiPathFrameworkRuntimeFactory()
    entrypoints = factory.discover_entrypoints()
    assert entrypoints == ["agent1"]

@pytest.mark.asyncio
async def test_factory_creates_runtime(tmp_path):
    # Setup config + agent file
    write_config(tmp_path, {"agents": {"agent1": "main.py:agent"}})
    write_agent_file(tmp_path / "main.py")
    os.chdir(tmp_path)

    factory = UiPathFrameworkRuntimeFactory()
    runtime = await factory.new_runtime("agent1", "test-runtime-id")
    assert runtime is not None

    schema = await runtime.get_schema()
    assert schema.type == "agent"

    await factory.dispose()

@pytest.mark.asyncio
async def test_factory_rejects_unknown_entrypoint(tmp_path):
    write_config(tmp_path, {"agents": {"agent1": "main.py:agent"}})
    os.chdir(tmp_path)

    factory = UiPathFrameworkRuntimeFactory()
    with pytest.raises(FrameworkRuntimeError):
        await factory.new_runtime("nonexistent", "test-id")
```

---

### STEP 8: Integration Testing with a Sample Agent

**Goal:** Create an end-to-end sample that validates the full integration works.

#### 8.1 Create a Sample Agent

```
samples/quickstart-agent/
├── pyproject.toml
├── {framework}.json
└── main.py
```

**`{framework}.json`:**
```json
{
  "agents": {
    "assistant": "main.py:agent"
  }
}
```

**`main.py`:** Create a simple agent using the target framework.

#### 8.2 End-to-End Tests

```python
# tests/test_e2e.py
import pytest

@pytest.mark.asyncio
async def test_full_lifecycle():
    """Test the complete runtime lifecycle."""
    factory = UiPathFrameworkRuntimeFactory()

    # 1. Discover
    entrypoints = factory.discover_entrypoints()
    assert len(entrypoints) > 0

    # 2. Create runtime
    runtime = await factory.new_runtime(entrypoints[0], "test-run")

    # 3. Schema
    schema = await runtime.get_schema()
    assert schema.input is not None
    assert schema.output is not None

    # 4. Execute
    result = await runtime.execute({"messages": "What is 2+2?"})
    assert result.status == UiPathRuntimeStatus.SUCCESSFUL
    assert result.output is not None

    # 5. Stream
    events = []
    async for event in runtime.stream({"messages": "What is 2+2?"}):
        events.append(event)
    assert len(events) >= 1
    assert isinstance(events[-1], UiPathRuntimeResult)

    # 6. Cleanup
    await runtime.dispose()
    await factory.dispose()
```

---

### STEP 9: LLM Provider Classes (Chat Module)

Most framework integrations need to route LLM calls through UiPath's LLM Gateway (AgentHub). This step shows how to create provider-specific LLM classes that transparently proxy requests through the gateway.

#### 9.1 Architecture

The `chat/` module provides drop-in LLM classes that users pass to their framework's agent constructor. Each class wraps a framework's native LLM class (or implements its base protocol) and redirects HTTP traffic through UiPath's gateway:

```
Framework Agent
  └── UiPathOpenAI / UiPathGemini / UiPathAnthropic  (your chat module)
        └── UiPath LLM Gateway
              └── Actual LLM provider (OpenAI, Google, Anthropic)
```

Gateway URL format: `{UIPATH_URL}/agenthub_/llm/raw/vendor/{vendor}/model/{model}/completions`

#### 9.2 Shared Utilities (`_common.py`)

Create a `_common.py` with three shared helpers used by all providers:

```python
# src/uipath_{framework}/chat/_common.py
import os
from uipath.utils import EndpointManager

def get_uipath_config() -> tuple[str, str]:
    """Read UIPATH_URL and UIPATH_ACCESS_TOKEN from environment."""
    uipath_url = os.getenv("UIPATH_URL")
    if not uipath_url:
        raise ValueError("UIPATH_URL environment variable is required")
    access_token = os.getenv("UIPATH_ACCESS_TOKEN")
    if not access_token:
        raise ValueError("UIPATH_ACCESS_TOKEN environment variable is required")
    return uipath_url, access_token

def get_uipath_headers(token: str) -> dict[str, str]:
    """Build auth + tracking headers for gateway requests."""
    headers = {"Authorization": f"Bearer {token}"}
    if job_key := os.getenv("UIPATH_JOB_KEY"):
        headers["X-UiPath-JobKey"] = job_key
    if process_key := os.getenv("UIPATH_PROCESS_KEY"):
        headers["X-UiPath-ProcessKey"] = process_key
    return headers

def build_gateway_url(vendor: str, model: str, uipath_url: str | None = None) -> str:
    """Build full gateway URL using EndpointManager."""
    if not uipath_url:
        uipath_url = os.getenv("UIPATH_URL")
    if not uipath_url:
        raise ValueError("UIPATH_URL environment variable is required")
    vendor_endpoint = EndpointManager.get_vendor_endpoint()
    formatted = vendor_endpoint.format(vendor=vendor, model=model)
    return f"{uipath_url.rstrip('/')}/{formatted}"
```

#### 9.3 Gateway Vendor Routing, API Flavor, and Model Naming

The gateway URL requires a **vendor** and **model** in the path. The correct vendor depends on the model name prefix:

| Model prefix | Vendor | API Flavor Header | Example model |
|---|---|---|---|
| `gpt-*`, `o1-*`, `o3-*` | `openai` | `OpenAiChatCompletions` | `gpt-4.1-2025-04-14` |
| `gemini-*` | `vertexai` | `GeminiGenerateContent` | `gemini-2.5-flash` |
| `anthropic.*` (Bedrock) | `awsbedrock` | `AnthropicClaude` | `anthropic.claude-haiku-4-5-20251001-v1:0` |
| `claude-*` (Vertex) | `vertexai` | `AnthropicClaude` | `claude-sonnet-4-20250514` |

**Required header**: Every request MUST include `X-UiPath-LlmGateway-ApiFlavor` so the gateway knows how to parse the request body. Without it, the gateway returns a 500 error.

**URL-rewriting is required**: Many provider SDKs append their own API paths to the `base_url` (e.g., the Anthropic SDK appends `/v1/messages`). This causes 404 errors because the gateway expects requests at `.../completions` exactly. Always use a URL-rewriting httpx transport instead of relying on `base_url`.

Example URL: `{UIPATH_URL}/agenthub_/llm/raw/vendor/awsbedrock/model/anthropic.claude-haiku-4-5-20251001-v1:0/completions`

#### 9.4 Three Integration Strategies

There are three strategies depending on what the framework's LLM class exposes:

**Strategy A: URL-Rewriting Transport with SDK Client** (e.g., Anthropic)

When the provider's SDK creates its own HTTP client, provide a custom `http_client` with a URL-rewriting transport:

```python
# src/uipath_{framework}/chat/anthropic.py
import httpx
from functools import cached_property
from google.adk.models.anthropic_llm import AnthropicLlm
from typing_extensions import override
from uipath._utils._ssl_context import get_httpx_client_kwargs
from ._common import build_gateway_url, get_uipath_config, get_uipath_headers

class _AsyncUrlRewriteTransport(httpx.AsyncBaseTransport):
    def __init__(self, gateway_url: str, **kwargs):
        self.gateway_url = gateway_url
        self._transport = httpx.AsyncHTTPTransport(**kwargs)

    async def handle_async_request(self, request):
        if "/v1/messages" in str(request.url):
            gateway_url_parsed = httpx.URL(self.gateway_url)
            headers = dict(request.headers)
            headers["host"] = gateway_url_parsed.host
            request = httpx.Request(
                method=request.method, url=self.gateway_url,
                headers=headers, content=request.content,
                extensions=request.extensions,
            )
        return await self._transport.handle_async_request(request)

class UiPathAnthropic(AnthropicLlm):
    @cached_property
    @override
    def _anthropic_client(self):
        from anthropic import AsyncAnthropic  # type: ignore[import-not-found]
        uipath_url, token = get_uipath_config()
        # anthropic.claude-* → Bedrock, claude-* → Vertex AI
        vendor = "awsbedrock" if self.model.startswith("anthropic.") else "vertexai"
        gateway_url = build_gateway_url(vendor, self.model, uipath_url)
        auth_headers = get_uipath_headers(token)
        auth_headers["X-UiPath-LlmGateway-ApiFlavor"] = "AnthropicClaude"
        client_kwargs = get_httpx_client_kwargs()
        http_client = httpx.AsyncClient(
            transport=_AsyncUrlRewriteTransport(gateway_url),
            headers=auth_headers, **client_kwargs,
        )
        return AsyncAnthropic(api_key=token, http_client=http_client)
```

**Key pattern**: The parent class (`AnthropicLlm`) creates its async client in a `cached_property`. Override that property to inject a custom `http_client` with a URL-rewriting transport. Do NOT use `base_url` — the Anthropic SDK appends `/v1/messages` to it, causing 404 errors. The rest of the class (content conversion, streaming, tool calling) works unchanged.

**Strategy B: URL-Rewriting httpx Transport** (for SDK clients that build URLs internally — e.g., Gemini)

When the provider's SDK builds URLs internally, intercept at the HTTP transport layer:

```python
# src/uipath_{framework}/chat/gemini.py
import httpx
from functools import cached_property
from google.adk.models.google_llm import Gemini
from google.genai import Client, types
from typing_extensions import override
from uipath._utils._ssl_context import get_httpx_client_kwargs
from ._common import build_gateway_url, get_uipath_config, get_uipath_headers

def _rewrite_request_for_gateway(request: httpx.Request, gateway_url: str) -> httpx.Request:
    """Rewrite generateContent URLs to the UiPath gateway."""
    url_str = str(request.url)
    if "generateContent" in url_str or "streamGenerateContent" in url_str:
        is_streaming = "streamGenerateContent" in url_str
        headers = dict(request.headers)
        if is_streaming:
            headers["X-UiPath-Streaming-Enabled"] = "true"
        gateway_url_parsed = httpx.URL(gateway_url)
        headers["host"] = gateway_url_parsed.host
        return httpx.Request(
            method=request.method, url=gateway_url,
            headers=headers, content=request.content,
            extensions=request.extensions,
        )
    return request

class _AsyncUrlRewriteTransport(httpx.AsyncBaseTransport):
    def __init__(self, gateway_url: str, **kwargs):
        self.gateway_url = gateway_url
        self._transport = httpx.AsyncHTTPTransport(**kwargs)

    async def handle_async_request(self, request):
        request = _rewrite_request_for_gateway(request, self.gateway_url)
        return await self._transport.handle_async_request(request)

class UiPathGemini(Gemini):
    @cached_property
    @override
    def api_client(self) -> Client:
        uipath_url, token = get_uipath_config()
        gateway_url = build_gateway_url("vertexai", self.model, uipath_url)
        auth_headers = get_uipath_headers(token)
        auth_headers["X-UiPath-LlmGateway-ApiFlavor"] = "GeminiGenerateContent"
        client_kwargs = get_httpx_client_kwargs()
        http_options = types.HttpOptions(
            headers=self._tracking_headers(),
            httpx_async_client=httpx.AsyncClient(
                transport=_AsyncUrlRewriteTransport(gateway_url),
                headers=auth_headers, **client_kwargs,
            ),
        )
        return Client(api_key="uipath-gateway", http_options=http_options)
```

**Key pattern**: The `Gemini` class creates a `google.genai.Client` in a `cached_property` called `api_client`. The `Client` constructor accepts `http_options` with custom httpx clients. Inject a transport that intercepts `generateContent`/`streamGenerateContent` requests and rewrites the URL to the gateway. All native Gemini features (streaming, caching, tool calling) work automatically.

**Strategy C: Raw HTTP + Full Content Conversion** (when no framework LLM class exists — e.g., OpenAI)

When the framework doesn't have a built-in class for the provider, implement `BaseLlm` directly:

```python
# src/uipath_{framework}/chat/openai.py
from google.adk.models.base_llm import BaseLlm
from google.genai import types

class UiPathOpenAI(BaseLlm):
    model: str = "gpt-4.1-2025-04-14"

    async def generate_content_async(self, llm_request, stream=False):
        # 1. Convert LlmRequest → OpenAI chat completions format
        body = self._build_request_body(llm_request, self.model, stream)
        # 2. POST to gateway via httpx
        # 3. Parse response (SSE for streaming) → yield LlmResponse
        ...
```

This requires implementing content conversion between the framework's types and the provider's API format. Key conversions:
- `types.Content` (role + parts) → OpenAI messages (role + content/tool_calls)
- `types.FunctionDeclaration` → OpenAI tools format
- `response_schema` → OpenAI `response_format` with `json_schema`
- SSE `data: {...}` lines → `LlmResponse` with partial content

> **Tip**: Look at the framework's existing LiteLLM integration (e.g., `google.adk.models.lite_llm`) for content conversion patterns. It speaks OpenAI format internally and has helpers like `_content_to_message_param()` and `_function_declaration_to_tool_param()` that you can use as reference.

#### 9.5 Lazy Imports for Optional Dependencies

If a provider SDK is an optional dependency (e.g., `anthropic`), use lazy imports at every level:

```python
# chat/__init__.py — lazy imports avoid loading optional deps at import time
def __getattr__(name):
    if name == "UiPathOpenAI":
        from .openai import UiPathOpenAI
        return UiPathOpenAI
    if name == "UiPathGemini":
        from .gemini import UiPathGemini
        return UiPathGemini
    if name == "UiPathAnthropic":
        from .anthropic import UiPathAnthropic
        return UiPathAnthropic
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = ["UiPathOpenAI", "UiPathGemini", "UiPathAnthropic"]
```

**Why**: Eager imports cause `ModuleNotFoundError` when optional dependencies aren't installed. The `__getattr__` pattern defers loading until the class is actually used.

In `pyproject.toml`, declare optional deps:
```toml
[project.optional-dependencies]
anthropic = ["anthropic>=0.43.0"]
```

#### 9.6 SSL Context

Always use `get_httpx_client_kwargs()` from `uipath._utils._ssl_context` when creating httpx clients. This provides the correct SSL context, timeout, and redirect settings:

```python
from uipath._utils._ssl_context import get_httpx_client_kwargs

client_kwargs = get_httpx_client_kwargs()
# Returns: {"follow_redirects": True, "timeout": 30.0, "verify": ssl_context}

async with httpx.AsyncClient(**client_kwargs) as client:
    ...
```

#### 9.7 Package Layout

```
src/uipath_{framework}/
├── __init__.py          # Lazy exports for LLM classes
├── chat/
│   ├── __init__.py      # Lazy imports (__getattr__ pattern)
│   ├── _common.py       # Shared: get_uipath_config, headers, gateway URL
│   ├── openai.py        # UiPathOpenAI(BaseLlm) — raw HTTP
│   ├── gemini.py        # UiPathGemini(Gemini) — URL-rewriting transport
│   └── anthropic.py     # UiPathAnthropic(AnthropicLlm) — URL-rewriting transport
└── runtime/
    └── ...              # Runtime integration (STEPs 1-8)
```

#### 9.8 Usage

```python
from uipath_google_adk.chat import UiPathOpenAI, UiPathGemini, UiPathAnthropic
from google.adk.agents import Agent

# OpenAI models via gateway
agent = Agent(name="assistant", model=UiPathOpenAI(model="gpt-4.1-2025-04-14"), ...)

# Gemini models via gateway
agent = Agent(name="assistant", model=UiPathGemini(model="gemini-2.5-flash"), ...)

# Anthropic models via gateway
agent = Agent(name="assistant", model=UiPathAnthropic(model="claude-sonnet-4-20250514"), ...)
```

#### 9.9 Checklist

- [ ] Shared `_common.py` with `get_uipath_config()`, `get_uipath_headers()`, `build_gateway_url()`
- [ ] Each provider class routes through UiPath gateway URL
- [ ] Correct **vendor** selected per model prefix (`openai`, `vertexai`, `awsbedrock` — see table in 9.3)
- [ ] `X-UiPath-LlmGateway-ApiFlavor` header set on every request (gateway returns 500 without it)
- [ ] URL-rewriting transport used (do NOT rely on SDK `base_url` — SDKs append paths causing 404s)
- [ ] Auth headers include `Authorization`, optional `X-UiPath-JobKey`, `X-UiPath-ProcessKey`
- [ ] Uses `EndpointManager.get_vendor_endpoint()` for URL construction
- [ ] Uses `get_httpx_client_kwargs()` for SSL/timeout when creating httpx clients
- [ ] Optional dependencies use lazy imports (`__getattr__`) at all package levels
- [ ] Optional deps declared in `pyproject.toml` under `[project.optional-dependencies]`
- [ ] mypy overrides added for optional import modules
- [ ] Streaming sets `X-UiPath-Streaming-Enabled: true` header
- [ ] All type annotations pass mypy (use `Dict[str, Any]` not bare `dict` for generic types)

---

## Quick Reference: Key Imports

```python
# Serialization (use this instead of writing your own _serialize.py)
from uipath.core.serialization import serialize_defaults

# Core protocols and types
from uipath.runtime import (
    UiPathRuntimeProtocol,           # Main protocol to implement
    UiPathRuntimeFactoryProtocol,     # Factory protocol to implement
    UiPathRuntimeFactoryRegistry,     # Register your factory
    UiPathRuntimeContext,             # Execution context
    UiPathExecuteOptions,             # Execute options (resume, breakpoints)
    UiPathStreamOptions,              # Stream options
    UiPathRuntimeResult,              # Execution result
    UiPathRuntimeStatus,              # SUCCESSFUL, FAULTED, SUSPENDED
    UiPathRuntimeEvent,               # Base event class
    UiPathRuntimeSchema,              # Schema container
    UiPathResumableRuntime,           # HITL wrapper
    UiPathResumeTrigger,              # Resume trigger model
    UiPathStreamNotSupportedError,    # If streaming not supported
    UiPathBreakpointResult,           # Breakpoint hit result
)

# Event types
from uipath.runtime.events import (
    UiPathRuntimeMessageEvent,        # AI messages, tool calls
    UiPathRuntimeStateEvent,          # State updates, node transitions
)

# Schema utilities
from uipath.runtime.schema import (
    UiPathRuntimeGraph,               # Graph container
    UiPathRuntimeNode,                # Graph node
    UiPathRuntimeEdge,                # Graph edge
    transform_references,             # Resolve $ref in JSON schema
    transform_nullable_types,         # Handle nullable types
    transform_attachments,            # Handle UiPathAttachment refs
)

# Error handling
from uipath.runtime.errors import (
    UiPathBaseRuntimeError,           # Base error class
    UiPathErrorCategory,              # DEPLOYMENT, SYSTEM, USER, UNKNOWN
    UiPathErrorCode,                  # Standard error codes
    UiPathErrorContract,              # Structured error info
)

# Storage (if HITL)
from uipath.runtime.storage import UiPathRuntimeStorageProtocol
from uipath.runtime.resumable.protocols import UiPathResumableStorageProtocol
```

---

## Framework-Specific Implementation Checklist

When implementing for a specific framework, check off these items:

### Schema Inference
- [ ] Uses `isinstance` checks (not `getattr`) to verify agent type before accessing I/O attributes
- [ ] Imports framework types from their actual module paths, not `__init__.py` re-exports
- [ ] Uses `uipath.runtime.schema.transform_references()` for `$ref` resolution (don't reimplement)
- [ ] Uses `uipath.runtime.schema.transform_nullable_types()` for nullable handling (don't reimplement)
- [ ] When agent has typed input (Pydantic model), uses it as the FULL input schema (replaces messages)
- [ ] When agent has typed output, uses it as the FULL output schema (replaces generic result)
- [ ] Falls back to `messages` input schema only for conversational agents without typed input
- [ ] Falls back to generic `{"result": any}` output schema only when no typed output exists
- [ ] Tool type parameters are strongly typed (e.g., `Callable | BaseTool | BaseToolset`)
- [ ] Builds visualization graph with correct node types

### Execution
- [ ] `execute()` runs agent and returns `UiPathRuntimeResult`
- [ ] Input `messages` field is correctly passed to framework
- [ ] Additional input fields are parsed into context/deps/config
- [ ] Output is serialized to dict via `uipath.core.serialization.serialize_defaults()` (do NOT create a custom `_serialize.py`)
- [ ] Errors are caught and wrapped in `FrameworkRuntimeError`

### Streaming
- [ ] `stream()` yields `UiPathRuntimeEvent` instances
- [ ] AI text/messages → `UiPathRuntimeMessageEvent`
- [ ] State transitions → `UiPathRuntimeStateEvent`
- [ ] Final event is always `UiPathRuntimeResult`
- [ ] Internal/bookkeeping events are filtered out

### HITL (if supported)
- [ ] Detects suspension signals from framework
- [ ] Saves state/context before suspending
- [ ] Returns `UiPathRuntimeStatus.SUSPENDED` with trigger info
- [ ] Can resume from saved state when `options.resume=True`
- [ ] SQLite storage for triggers and KV data

### Factory
- [ ] Reads config file (`{framework}.json`)
- [ ] Discovers entrypoints from config
- [ ] Loads agents dynamically from `file.py:variable` paths
- [ ] Caches loaded agents for reuse
- [ ] Supports multiple agent definition patterns (instance, function, async, context manager)
- [ ] Registers via `UiPathRuntimeFactoryRegistry.register()`
- [ ] Entry point in `pyproject.toml`
- [ ] `dispose()` cleans up all resources

### Testing
- [ ] Schema inference tests (input, output, graph)
- [ ] Config parsing tests (valid, invalid, missing)
- [ ] Loader tests (security, resolution patterns)
- [ ] Execute tests (success, error handling)
- [ ] Streaming tests (event types, final result)
- [ ] Storage tests (if HITL)
- [ ] E2E lifecycle test

---

## Existing Integrations as Reference

| Integration | Config File | Runner API | HITL | Package |
|-------------|------------|------------|------|---------|
| **OpenAI Agents** | `openai_agents.json` | `Runner.run()` / `Runner.run_streamed()` | No | `uipath-openai-agents` |
| **LlamaIndex** | `llama_index.json` | `workflow.run()` / `handler.stream_events()` | Yes (`InputRequiredEvent`) | `uipath-llamaindex` |
| **LangGraph** | `langgraph.json` | `graph.ainvoke()` / `graph.astream()` | Yes (`interrupt()`, `Command(resume=)`) | `uipath-langchain` |

Source code locations (in this monorepo):
- **OpenAI Agents**: `packages/uipath-openai-agents/src/uipath_openai_agents/runtime/`
- **LlamaIndex**: `packages/uipath-llamaindex/src/uipath_llamaindex/runtime/`
- **LangGraph**: See `uipath-langchain-python` repo: `src/uipath_langchain/runtime/`
- **Runtime contracts**: `uipath-runtime-python` repo, or installed at `.venv/Lib/site-packages/uipath/runtime/`

LLM Gateway chat module locations:
- **Google ADK**: `packages/uipath-google-adk/src/uipath_google_adk/chat/` (OpenAI, Gemini, Anthropic)
- **OpenAI Agents**: `packages/uipath-openai-agents/src/uipath_openai_agents/chat/` (OpenAI)
- **LlamaIndex**: `packages/uipath-llamaindex/src/uipath_llamaindex/llms/` (Vertex/Gemini)
