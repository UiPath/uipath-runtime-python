# UiPath Runtime Integration Genome

> A complete blueprint for Claude Code to autonomously build a new UiPath runtime integration
> for any Python agentic framework.

---

## How This Document Works

This is not documentation for humans to read and implement manually. This is a **genome** — a
structured specification that Claude Code reads and executes step-by-step to produce a working
integration package. Each phase has inputs, instructions, quality gates, and reference code
from real integrations.

**Execution model:**
1. Phase 0 — Ask questions about the target framework
2. Phase 1 — Analyze the framework and map to UiPath contracts
3. Phase 2 — Build the integration step-by-step (9 steps, with quality gates)
4. Phase 3 — Polish (tests, CLI middleware, samples, README)

---

## Architecture Overview

### What an Integration Is

An integration is a Python package (`uipath-<framework>`) that bridges a third-party agentic
framework with the UiPath platform. It implements two core protocols:

- **`UiPathRuntimeProtocol`** — execute agents, stream events, provide schemas
- **`UiPathRuntimeFactoryProtocol`** — discover entrypoints, create runtime instances

### The Runtime Wrapper Stack

The CLI automatically wraps your runtime with cross-cutting concerns. **You only implement
the innermost layer.** The CLI handles the rest based on execution context:

```
┌─ UiPathExecutionRuntime        ← Tracing/telemetry      [CLI: eval mode]
│  └─ UiPathChatRuntime          ← Chat bridge (CAS)      [CLI: job_id + conversation_id]
│     └─ UiPathDebugRuntime      ← Debugger/breakpoints   [CLI: debug command]
│        └─ UiPathResumableRuntime ← HITL/resume triggers  [Factory: if HITL supported]
│           └─ YourFrameworkRuntime ← YOUR CODE            [Factory: always]
```

**Who applies each wrapper:**

| Wrapper | Applied By | Condition |
|---------|-----------|-----------|
| `UiPathExecutionRuntime` | CLI (`cli_eval`) | Evaluation runs — adds OpenTelemetry spans, execution logging |
| `UiPathChatRuntime` | CLI (`cli_run`, `cli_debug`) | Cloud execution with `job_id` AND `conversation_id` — bridges chat events to CAS via WebSocket |
| `UiPathDebugRuntime` | CLI (`cli_debug`) | Debug mode — manages breakpoints, state event emission, trigger polling via SignalR or console bridge |
| `UiPathResumableRuntime` | Your Factory | When your framework supports HITL — manages resume triggers and storage |
| `YourFrameworkRuntime` | Your Factory | Always — your core implementation |

**You never instantiate the outer wrappers.** Your factory creates `YourFrameworkRuntime`,
optionally wraps it with `UiPathResumableRuntime`, and returns it. The CLI does the rest.

### Graph Visualization and State Events

Your integration participates in the debugging/visualization system by:

1. **Providing a graph** via `get_schema()` — nodes and edges that describe the agent's structure
2. **Emitting state events** during `stream()` — `STARTED`, `UPDATED`, `COMPLETED`, `FAULTED`
   phases for each node as it executes
3. **Supporting breakpoints** — node IDs from the graph can be used as breakpoint targets

The debug bridge (console or SignalR) receives state events and breakpoint results to provide
a visual debugging experience. The `UiPathDebugRuntime` wrapper intercepts these events from
your `stream()` output and forwards them to the bridge.

**State event lifecycle per node:**
```
STARTED  →  UPDATED (0..N times)  →  COMPLETED
                                  →  FAULTED (if error)
```

**Breakpoint modes:**
- `breakpoints: ["node_a", "node_b"]` — suspend before specific nodes
- `breakpoints: "*"` — step mode, suspend before every node
- `breakpoints: None` — no breakpoints, free-running

---

## Phase 0 — Framework Discovery

> Goal: Understand the target framework to determine what to build.

Ask the user these questions. Use the answers to populate `integration_config.yaml`.

### Questions

**Q1: Framework Identity**
```
What framework are you integrating?
- Name (e.g., "CrewAI", "AutoGen", "PydanticAI")
- PyPI package name (e.g., "crewai", "autogen-agentchat", "pydantic-ai")
- Import path (e.g., "crewai", "autogen_agentchat", "pydantic_ai")
```

**Q2: Agent Definition Pattern**
```
How are agents/workflows defined in this framework?
a) Class instantiation — Agent(name="...", tools=[...])
b) Decorator/function — @agent def my_agent(): ...
c) Configuration file — YAML/JSON that declares agents
d) Graph/workflow builder — StateGraph().add_node(...)
```

**Q3: Execution API**
```
How do you run an agent in this framework?
- Method name for synchronous execution (e.g., agent.run(), crew.kickoff())
- Method name for async execution (e.g., agent.arun(), await runner.run())
- What does it return? (string, object, custom result type)
```

**Q4: Streaming Support**
```
Does the framework support streaming execution events?
a) Yes — async generator / async iterator (e.g., agent.astream(), runner.run_streamed())
b) Yes — callback/event hooks (e.g., on_tool_start, on_llm_end)
c) No — only synchronous execution
```

**Q5: HITL / Interrupt Support**
```
Does the framework support pausing execution for human input?
a) Yes — built-in interrupt/checkpoint system (e.g., LangGraph interrupts, workflow events)
b) Yes — tool-level callbacks that can block (e.g., human_input tool)
c) No — runs to completion without interruption
```

**Q6: Message Format**
```
What message format does the framework use internally?
a) OpenAI-style — {"role": "user", "content": "..."}
b) Framework-specific message objects (e.g., HumanMessage, AIMessage)
c) Simple strings
d) Custom content types (e.g., google.genai.types.Content)
```

**Q7: Typed Input/Output**
```
Does the agent define typed input/output schemas?
a) Yes — Pydantic models or type hints on the agent definition
b) Partial — input types but output is unstructured
c) No — accepts/returns generic dicts or strings
```

### Output: integration_config.yaml

```yaml
framework:
  name: "CrewAI"
  package: "crewai"
  import_path: "crewai"
  config_file: "crewai.json"

agent_definition: "class"           # class | function | config | graph
execution_api:
  sync: "crew.kickoff"
  async: "crew.akickoff"
  return_type: "CrewOutput"
streaming: "callback"               # async_generator | callback | none
hitl: "none"                        # builtin | tool_callback | none
message_format: "openai"            # openai | framework | string | custom
typed_schemas: "partial"            # yes | partial | no

capability_tier: "CORE"             # Derived: FULL | CORE | MINIMAL
```

### Capability Tier Derivation

```
IF streaming != "none" AND hitl == "builtin":
    tier = FULL
ELIF streaming != "none":
    tier = CORE
ELSE:
    tier = MINIMAL
```

| Tier | Files to Generate |
|------|------------------|
| **FULL** | config, loader, schema, runtime (execute + stream), storage, resumable, factory, errors, CLI middleware, tests |
| **CORE** | config, loader, schema, runtime (execute + stream), factory, errors, CLI middleware, tests |
| **MINIMAL** | config, loader, schema, runtime (execute only), factory, errors, CLI middleware, tests |

---

## Phase 1 — Architecture Mapping

> Goal: Map the target framework's concepts to UiPath runtime contracts.

### Concept Mapping Table

Fill this out based on Phase 0 answers:

```
Framework Concept          → UiPath Contract
─────────────────────────────────────────────
Agent/Crew/Workflow        → Entrypoint (string in config JSON)
agent.run() / crew.kickoff → runtime.execute()
agent.astream()            → runtime.stream()
Agent class definition     → get_schema() input/output
Agent tools list           → UiPathRuntimeNode (type="tool")
Agent sub-agents           → UiPathRuntimeNode (type="agent") with subgraph
Framework events           → UiPathRuntimeStateEvent / UiPathRuntimeMessageEvent
Framework result           → UiPathRuntimeResult.output
Framework errors           → UiPathBaseRuntimeError with error codes
Framework interrupts       → UiPathRuntimeStatus.SUSPENDED + UiPathResumeTrigger
Config file (JSON)         → Factory.discover_entrypoints()
```

### Package Structure to Generate

```
packages/uipath-<framework>/
├── src/uipath_<framework>/
│   ├── __init__.py
│   ├── middlewares.py                  # CLI middleware registration
│   ├── _cli/
│   │   ├── __init__.py
│   │   ├── cli_new.py                 # Scaffolding command
│   │   └── _templates/
│   │       ├── main.py.template       # Starter agent template
│   │       └── config.json.template   # Config file template
│   ├── chat/                          # [Optional] LLM provider classes
│   │   ├── __init__.py
│   │   ├── openai.py                  # OpenAI via UiPath Gateway
│   │   ├── anthropic.py              # Anthropic via UiPath Gateway
│   │   └── gemini.py                 # Gemini via UiPath Gateway
│   └── runtime/
│       ├── __init__.py
│       ├── config.py                  # Config file parser
│       ├── loader.py                  # Dynamic agent loader
│       ├── schema.py                  # Schema + graph extraction
│       ├── runtime.py                 # UiPathRuntimeProtocol impl
│       ├── factory.py                 # UiPathRuntimeFactoryProtocol impl
│       ├── errors.py                  # Error codes and exception classes
│       ├── messages.py                # [CORE+] Message format mapper
│       └── storage.py                # [FULL only] SQLite storage adapter
├── tests/
│   ├── __init__.py
│   ├── conftest.py
│   ├── test_schema.py
│   ├── test_runtime.py
│   └── test_factory.py
├── samples/
│   └── simple_agent/
│       ├── main.py
│       └── <framework>.json
├── pyproject.toml
└── README.md
```

---

## Phase 2 — Build Execution

> 9 steps. Each has instructions, reference code, and a quality gate.

---

### Step 1: Project Scaffold

> Create the package structure, pyproject.toml, and basic module files.

#### pyproject.toml Template

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.backends"

[project]
name = "uipath-<framework>"
version = "0.0.1"
description = "UiPath integration for <Framework>"
requires-python = ">=3.11"
dependencies = [
    "uipath>=2.8.35, <2.9.0",
    "uipath-runtime>=0.9.0, <1.0.0",
    "<framework-package>>=X.Y.Z, <X+1.0.0",
]

[project.entry-points."uipath.runtime.factories"]
<framework-id> = "uipath_<framework>.runtime:register_runtime_factory"

[project.entry-points."uipath.middlewares"]
register = "uipath_<framework>.middlewares:register_middleware"

[tool.hatch.build.targets.wheel]
packages = ["src/uipath_<framework>"]

[tool.ruff]
line-length = 88

[tool.ruff.format]
quote-style = "double"

[tool.mypy]
python_version = "3.11"
strict = true
plugins = ["pydantic.mypy"]

[[tool.mypy.overrides]]
module = "<framework_import>.*"
ignore_missing_imports = true
```

#### Entry Points Explained

The two entry points are critical:

1. **`uipath.runtime.factories`** — Registers your factory so the CLI can discover it when
   your config file (e.g., `crewai.json`) exists in a project directory. The CLI calls
   `UiPathRuntimeFactoryRegistry.get()` which iterates over all registered factories and
   picks the one whose config file is present.

2. **`uipath.middlewares`** — Registers your CLI middleware so `uipath new` can offer your
   framework as a scaffolding option. When a user runs `uipath new`, the CLI calls all
   registered middlewares which can inject their own commands.

#### __init__.py Template

```python
"""UiPath <Framework> Integration."""

def __getattr__(name: str):
    """Lazy imports for optional heavy dependencies."""
    if name == "UiPath<Framework>RuntimeFactory":
        from .runtime.factory import UiPath<Framework>RuntimeFactory
        return UiPath<Framework>RuntimeFactory
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
```

#### QUALITY GATE 1
```
✓ Package installs with `uv pip install -e .`
✓ Entry points are discoverable: `python -c "from importlib.metadata import entry_points; print(entry_points(group='uipath.runtime.factories'))"`
✓ Directory structure matches the template above
✓ No import errors on `import uipath_<framework>`
```

---

### Step 2: Config & Loader

> Parse the framework's config file and dynamically load agents.

#### Config File Format

Every integration uses a JSON config file that maps entrypoint names to Python module paths:

```json
{
  "agents": {
    "my_agent": "main.py:agent",
    "another": "src/agents/advanced.py:workflow"
  }
}
```

The key is the entrypoint name (used in CLI: `uipath run my_agent`), the value is
`<file_path>:<variable_name>` pointing to the agent/workflow object.

**Naming convention:** Use `agents` as the key for agent-based frameworks, `workflows` for
workflow-based frameworks (like LlamaIndex).

#### config.py — Reference Implementation

```python
"""Configuration loader for <Framework> integration."""

from __future__ import annotations

import json
from pathlib import Path


class <Framework>Config:
    """Loads and caches <framework>.json configuration."""

    def __init__(self, config_path: str = "<framework>.json") -> None:
        self._config_path = config_path
        self._agents: dict[str, str] | None = None

    @property
    def agents(self) -> dict[str, str]:
        if self._agents is None:
            self._agents = self._load_agents()
        return self._agents

    def _load_agents(self) -> dict[str, str]:
        config_file = Path(self._config_path)
        if not config_file.exists():
            raise FileNotFoundError(
                f"Config file not found: {self._config_path}. "
                f"Run 'uipath init' to generate it."
            )
        with open(config_file) as f:
            data = json.load(f)
        return data.get("agents", {})
```

#### loader.py — Reference Implementation

The loader dynamically imports the Python module and extracts the agent variable.
It must handle both sync and async agent constructors, and support context managers.

```python
"""Dynamic agent/workflow loader for <Framework> integration."""

from __future__ import annotations

import importlib
import inspect
import sys
from pathlib import Path
from typing import Any, Self


class <Framework>AgentLoader:
    """Loads an agent from a module_path:variable_name string."""

    def __init__(self, module_path: str, variable_name: str) -> None:
        self._module_path = module_path
        self._variable_name = variable_name
        self._agent: Any = None

    @classmethod
    def from_entrypoint(cls, entrypoint: str, base_path: str = ".") -> Self:
        """Parse 'path/to/file.py:variable' format."""
        if ":" not in entrypoint:
            raise ValueError(
                f"Invalid entrypoint format: {entrypoint}. "
                f"Expected 'path/to/file.py:variable_name'"
            )
        module_path, variable_name = entrypoint.rsplit(":", 1)
        # Resolve relative to base_path
        full_path = str(Path(base_path) / module_path)
        return cls(full_path, variable_name)

    async def load(self) -> Any:
        """Load and return the agent object."""
        # Add parent directory to sys.path for imports
        module_dir = str(Path(self._module_path).parent.resolve())
        if module_dir not in sys.path:
            sys.path.insert(0, module_dir)

        # Import the module
        module_name = Path(self._module_path).stem
        spec = importlib.util.spec_from_file_location(
            module_name, self._module_path
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load module: {self._module_path}")

        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        # Get the variable
        agent = getattr(module, self._variable_name, None)
        if agent is None:
            raise AttributeError(
                f"Module {self._module_path} has no attribute "
                f"'{self._variable_name}'"
            )

        # Handle callables (factory functions)
        if callable(agent) and not isinstance(agent, type):
            agent = agent() if not inspect.iscoroutinefunction(agent) else await agent()

        # Handle async context managers
        if hasattr(agent, "__aenter__"):
            agent = await agent.__aenter__()

        self._agent = agent
        return agent

    async def cleanup(self) -> None:
        """Cleanup resources (async context managers)."""
        if self._agent and hasattr(self._agent, "__aexit__"):
            await self._agent.__aexit__(None, None, None)
```

#### errors.py — Reference Implementation

```python
"""Error codes and exceptions for <Framework> integration."""

from __future__ import annotations

from enum import Enum

from uipath.runtime.errors import UiPathBaseRuntimeError, UiPathErrorCategory


class UiPath<Framework>ErrorCode(Enum):
    """Error codes for <Framework> runtime errors."""

    AGENT_LOAD_FAILURE = "<FRAMEWORK>.AGENT_LOAD_FAILURE"
    AGENT_EXECUTION_ERROR = "<FRAMEWORK>.AGENT_EXECUTION_ERROR"
    AGENT_TIMEOUT = "<FRAMEWORK>.AGENT_TIMEOUT"
    CONFIG_MISSING = "<FRAMEWORK>.CONFIG_MISSING"
    CONFIG_INVALID = "<FRAMEWORK>.CONFIG_INVALID"
    SCHEMA_INFERENCE_ERROR = "<FRAMEWORK>.SCHEMA_INFERENCE_ERROR"
    STREAM_ERROR = "<FRAMEWORK>.STREAM_ERROR"


class UiPath<Framework>RuntimeError(UiPathBaseRuntimeError):
    """Runtime error for <Framework> integration."""

    def __init__(
        self,
        code: UiPath<Framework>ErrorCode,
        title: str,
        detail: str,
        category: UiPathErrorCategory,
        status: int | None = None,
    ) -> None:
        super().__init__(
            code=code.value,
            title=title,
            detail=detail,
            category=category,
            status=status,
            prefix="<Framework>",
        )
```

#### QUALITY GATE 2
```
✓ Config parser loads a sample <framework>.json without errors
✓ Loader can import a simple Python module and extract a variable
✓ Error codes are properly defined with unique prefixes
✓ from_entrypoint("main.py:agent") correctly parses the string
```

---

### Step 3: Schema Inference

> Extract input/output JSON schemas and build a graph for visualization.

Schema inference serves two purposes:
1. **Input/Output schemas** — used by the platform to validate data and generate UIs
2. **Graph structure** — used by the debugger for visualization and breakpoint targeting

#### Key Principles

1. **Use `isinstance` checks, not `getattr` guessing** — Verify actual agent types before
   accessing framework-specific attributes.
2. **Import from actual module paths** — Not from `__init__.py` re-exports which may be unstable.
3. **Use helper utilities** — `transform_references()`, `transform_nullable_types()`,
   `transform_attachments()` from `uipath.runtime.schema`.
4. **Typed input replaces messages** — If agent defines `input_schema`, use it as the FULL
   input (don't merge with a generic messages field).
5. **Typed output replaces generic result** — If agent defines `output_schema`, use it as
   the FULL output.
6. **Fallback to defaults** — Only use `{"messages": string}` for conversational agents
   WITHOUT typed input.

#### Default Schemas (Conversational Agent)

When the agent has no typed input/output, use the conversational default:

```python
DEFAULT_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "messages": {"type": "string", "description": "User message"}
    },
    "required": ["messages"],
}

DEFAULT_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "messages": {"type": "string", "description": "Agent response"}
    },
    "required": ["messages"],
}
```

#### Graph Building

The graph describes the agent's structure for debugging visualization. Each node in the graph
can be a breakpoint target (using `node.id`).

```python
from uipath.runtime.schema import (
    UiPathRuntimeGraph,
    UiPathRuntimeNode,
    UiPathRuntimeEdge,
    UiPathRuntimeSchema,
    transform_references,
    transform_nullable_types,
    transform_attachments,
)


def build_graph(agent: Any) -> UiPathRuntimeGraph | None:
    """Build a visualization graph from the framework agent.

    Node IDs become breakpoint targets. The debug bridge uses them to:
    - Display execution progress (which node is running)
    - Set breakpoints (pause before/after specific nodes)
    - Show the agent's structure in the UI
    """
    nodes: list[UiPathRuntimeNode] = []
    edges: list[UiPathRuntimeEdge] = []

    # Example: Extract tools as nodes
    for tool in getattr(agent, "tools", []):
        nodes.append(UiPathRuntimeNode(
            id=tool.name,               # ← This ID is used for breakpoints
            name=tool.name,
            type="tool",
            metadata={"description": getattr(tool, "description", None)},
        ))

    # Example: Extract sub-agents as nodes with subgraphs
    for sub_agent in getattr(agent, "agents", []):
        sub_graph = build_graph(sub_agent)  # Recursive
        nodes.append(UiPathRuntimeNode(
            id=sub_agent.name,
            name=sub_agent.name,
            type="agent",
            subgraph=sub_graph,         # ← Nested graph for composite nodes
        ))

    # Example: Add the main agent node
    nodes.insert(0, UiPathRuntimeNode(
        id="agent",
        name=agent.name if hasattr(agent, "name") else "agent",
        type="model",
        metadata={"model": getattr(agent, "model", None)},
    ))

    # Build edges (agent → tools, agent → sub-agents)
    for node in nodes[1:]:
        edges.append(UiPathRuntimeEdge(
            source="agent",
            target=node.id,
            label=None,
        ))

    if not nodes:
        return None

    return UiPathRuntimeGraph(nodes=nodes, edges=edges)
```

#### Schema Types Reference

```python
class UiPathRuntimeNode(BaseModel):
    id: str                                    # Unique ID — used as breakpoint target
    name: str                                  # Display name
    type: str                                  # "tool", "model", "agent", "node"
    subgraph: UiPathRuntimeGraph | None = None # Nested graph for hierarchical agents
    metadata: dict[str, Any] | None = None     # Framework-specific metadata

class UiPathRuntimeEdge(BaseModel):
    source: str                                # Source node ID
    target: str                                # Target node ID
    label: str | None = None                   # Condition or transition label

class UiPathRuntimeGraph(BaseModel):
    nodes: list[UiPathRuntimeNode]
    edges: list[UiPathRuntimeEdge]

class UiPathRuntimeSchema(BaseModel):
    file_path: str                             # Entrypoint identifier
    unique_id: str                             # UUID for this schema
    type: str                                  # "agent", "workflow", etc.
    input: dict[str, Any]                      # Input JSON schema
    output: dict[str, Any]                     # Output JSON schema
    graph: UiPathRuntimeGraph | None = None    # Visualization graph
    metadata: dict[str, Any] | None = None     # Custom metadata
```

#### Complete get_schema() Implementation Pattern

```python
import uuid
from uipath.runtime.schema import (
    UiPathRuntimeSchema,
    transform_references,
    transform_nullable_types,
    transform_attachments,
)


async def get_schema(self) -> UiPathRuntimeSchema:
    """Extract schema from the loaded agent."""
    # 1. Try typed input schema
    input_schema = self._get_typed_input_schema()
    if input_schema:
        input_schema = transform_attachments(
            transform_nullable_types(
                transform_references(input_schema)
            )
        )
    else:
        input_schema = DEFAULT_INPUT_SCHEMA

    # 2. Try typed output schema
    output_schema = self._get_typed_output_schema()
    if output_schema:
        output_schema = transform_attachments(
            transform_nullable_types(
                transform_references(output_schema)
            )
        )
    else:
        output_schema = DEFAULT_OUTPUT_SCHEMA

    # 3. Build visualization graph
    graph = build_graph(self._agent)

    return UiPathRuntimeSchema(
        file_path=self._entrypoint,
        unique_id=str(uuid.uuid4()),
        type="agent",
        input=input_schema,
        output=output_schema,
        graph=graph,
    )
```

#### Reference: How Existing Integrations Build Graphs

**OpenAI Agents** — Extracts agents, handoffs (sub-agents), tools:
```python
# Agents become nodes with type="agent"
# Handoffs become edges between agent nodes
# Tools become nodes with type="tool"
# Nested agents get subgraph with recursive extraction
```

**LangGraph** — Extracts from compiled StateGraph:
```python
# Graph nodes → UiPathRuntimeNode (type detected from node content)
# Graph edges → UiPathRuntimeEdge (conditional edges get labels)
# Subgraphs → nested UiPathRuntimeGraph via node.subgraph
```

**Google ADK** — Extracts from LlmAgent tree:
```python
# Agent → node with type="model"
# sub_agents → recursive nodes with subgraphs
# tools → nodes with type="tool"
```

#### QUALITY GATE 3
```
✓ get_schema() returns valid UiPathRuntimeSchema
✓ input/output schemas are valid JSON Schema
✓ Graph nodes have unique IDs (these become breakpoint targets)
✓ Graph edges reference valid source/target node IDs
✓ transform_references() resolves all $ref pointers
✓ Typed schemas override defaults when present
```

---

### Step 4: Execute Implementation

> Implement `runtime.execute()` — synchronous agent execution.

#### Core Contract

```python
async def execute(
    self,
    input: dict[str, Any],
    options: UiPathExecuteOptions,
) -> UiPathRuntimeResult:
```

**Input:** Always a `dict[str, Any]` parsed from JSON. Your runtime must map this to
whatever the framework expects.

**Output:** Always a `UiPathRuntimeResult` with:
- `output` — JSON-serializable result (use `serialize_json()`)
- `status` — `SUCCESSFUL`, `FAULTED`, or `SUSPENDED`
- `error` — `UiPathErrorContract` if faulted

#### UiPathExecuteOptions

```python
class UiPathExecuteOptions(BaseModel):
    resume: bool = False                                    # Resume from suspended state
    breakpoints: list[str] | Literal["*"] | None = None    # Breakpoint node IDs

    model_config = ConfigDict(extra="allow")  # Accepts arbitrary extra fields
```

The `breakpoints` field is populated by `UiPathDebugRuntime` from the debug bridge.
Your runtime should check this and yield `UiPathBreakpointResult` when a node matches.
If your framework doesn't support breakpoints natively, the debug wrapper handles it
via stream interception — but you should still emit state events so the wrapper knows
which node is executing.

#### Implementation Pattern

```python
import traceback
from typing import Any

from uipath.core.serialization import serialize_json
from uipath.runtime import (
    UiPathExecuteOptions,
    UiPathRuntimeResult,
    UiPathRuntimeStatus,
)
from uipath.runtime.errors import UiPathErrorCategory

from .errors import UiPath<Framework>ErrorCode, UiPath<Framework>RuntimeError


class UiPath<Framework>Runtime:
    """UiPath runtime implementation for <Framework>."""

    def __init__(
        self,
        agent: Any,
        runtime_id: str,
        entrypoint: str,
    ) -> None:
        self._agent = agent
        self._runtime_id = runtime_id
        self._entrypoint = entrypoint

    async def execute(
        self,
        input: dict[str, Any],
        options: UiPathExecuteOptions,
    ) -> UiPathRuntimeResult:
        """Execute the agent and return result."""
        try:
            # 1. Map input dict to framework format
            framework_input = self._map_input(input)

            # 2. Run the agent
            result = await self._run_agent(framework_input, options)

            # 3. Serialize output
            output = serialize_json(result)

            return UiPathRuntimeResult(
                output=output,
                status=UiPathRuntimeStatus.SUCCESSFUL,
            )
        except UiPath<Framework>RuntimeError:
            raise  # Re-raise our own errors
        except Exception as e:
            raise UiPath<Framework>RuntimeError(
                code=UiPath<Framework>ErrorCode.AGENT_EXECUTION_ERROR,
                title="Agent execution failed",
                detail=f"{type(e).__name__}: {e}\n{traceback.format_exc()}",
                category=UiPathErrorCategory.SYSTEM,
            ) from e

    def _map_input(self, input: dict[str, Any]) -> Any:
        """Map UiPath input dict to framework-specific format."""
        # For conversational agents:
        if "messages" in input:
            return input["messages"]  # or convert to framework message type
        # For typed agents:
        return input

    async def _run_agent(self, input: Any, options: UiPathExecuteOptions) -> Any:
        """Run the framework agent. Override per framework."""
        # Example patterns:
        # return await self._agent.arun(input)
        # return await self._agent.kickoff(inputs=input)
        # return await Runner.run(self._agent, input)
        raise NotImplementedError
```

#### Serialization Rules

Always use `serialize_json()` from `uipath.core.serialization`. It handles:
- Pydantic models → dict
- Dataclasses → dict
- Enums → value
- datetime → ISO string
- Custom objects → best-effort serialization

```python
from uipath.core.serialization import serialize_json

# DO:
output = serialize_json(framework_result)

# DON'T:
output = framework_result.model_dump()  # May miss nested types
output = json.loads(json.dumps(result, default=str))  # Lossy
```

#### Resume Support (for HITL)

When `options.resume` is `True`, the agent should resume from its last checkpoint
rather than starting fresh. How this works depends on the framework:

```python
async def execute(self, input, options):
    if options.resume:
        # Resume from checkpoint
        result = await self._resume_agent(input, options)
    else:
        # Fresh execution
        result = await self._run_agent(self._map_input(input), options)
    ...
```

#### QUALITY GATE 4
```
✓ execute() returns UiPathRuntimeResult with status SUCCESSFUL on happy path
✓ execute() returns UiPathRuntimeResult with status FAULTED on error (via exception)
✓ Output is serialized via serialize_json()
✓ Errors are wrapped in UiPath<Framework>RuntimeError with proper category
✓ Input mapping correctly handles both "messages" and typed inputs
✓ Resume flag is respected (if HITL supported)
```

---

### Step 5: Streaming Events

> Implement `runtime.stream()` — real-time event emission during execution.

**Skip this step if capability tier is MINIMAL.**

#### Core Contract

```python
async def stream(
    self,
    input: dict[str, Any],
    options: UiPathStreamOptions,
) -> AsyncGenerator[UiPathRuntimeEvent, None]:
```

Your `stream()` must yield events as the agent executes, and **always yield a
`UiPathRuntimeResult` as the final event**.

#### Event Types

There are two event types you emit during streaming:

**1. `UiPathRuntimeStateEvent`** — Node lifecycle events for graph visualization

```python
from uipath.runtime.events import UiPathRuntimeStateEvent, UiPathRuntimeStatePhase

# When a node starts executing:
yield UiPathRuntimeStateEvent(
    payload={"input": node_input},          # State data (what went into the node)
    node_name="agent_node",                 # Simple node name
    qualified_node_name="parent:agent_node",# Full path (for subgraph hierarchy)
    phase=UiPathRuntimeStatePhase.STARTED,  # Node is starting
)

# When a node's state updates mid-execution:
yield UiPathRuntimeStateEvent(
    payload={"partial_output": partial},
    node_name="agent_node",
    qualified_node_name="parent:agent_node",
    phase=UiPathRuntimeStatePhase.UPDATED,
)

# When a node completes:
yield UiPathRuntimeStateEvent(
    payload={"output": node_output},
    node_name="agent_node",
    qualified_node_name="parent:agent_node",
    phase=UiPathRuntimeStatePhase.COMPLETED,
)

# When a node fails:
yield UiPathRuntimeStateEvent(
    payload={"error": str(error)},
    node_name="agent_node",
    qualified_node_name="parent:agent_node",
    phase=UiPathRuntimeStatePhase.FAULTED,
)
```

**Important:** The `node_name` values must correspond to `UiPathRuntimeNode.id` values
from your graph (Step 3). This is how the debugger knows which graph node is currently
executing, and how breakpoints match.

**`qualified_node_name`** includes the subgraph hierarchy, separated by colons. For a
top-level node, it equals `node_name`. For a nested node: `"parent_graph:child_node"`.

**2. `UiPathRuntimeMessageEvent`** — AI messages, tool calls, text chunks

```python
from uipath.runtime.events import UiPathRuntimeMessageEvent

# When the LLM produces output (text chunk, tool call, etc.)
yield UiPathRuntimeMessageEvent(
    payload=message_object,  # Framework-specific message object
)
```

The `payload` of message events is framework-specific. The `UiPathChatRuntime` wrapper
(applied by the CLI) will consume these and forward them to the chat bridge for UI display.

#### Breakpoint Support in Streaming

When `options.breakpoints` is set, your stream should check if the current node matches
a breakpoint and yield a `UiPathBreakpointResult`:

```python
from uipath.runtime import UiPathBreakpointResult, UiPathStreamOptions


async def stream(self, input, options: UiPathStreamOptions):
    breakpoints = options.breakpoints if options else None

    for node in execution_nodes:
        # Check if this node is a breakpoint target
        if breakpoints is not None:
            if breakpoints == "*" or node.id in breakpoints:
                yield UiPathBreakpointResult(
                    breakpoint_node=node.id,
                    breakpoint_type="before",
                    current_state=current_state_snapshot,
                    next_nodes=[next_node.id for next_node in node.successors],
                )
                # After yielding breakpoint, the UiPathDebugRuntime wrapper
                # will wait for resume, then call stream() again with
                # options.resume=True and updated breakpoints

        # Emit STARTED
        yield UiPathRuntimeStateEvent(
            payload={}, node_name=node.id,
            phase=UiPathRuntimeStatePhase.STARTED,
        )

        # Execute node...
        result = await node.execute()

        # Emit COMPLETED
        yield UiPathRuntimeStateEvent(
            payload={"output": result}, node_name=node.id,
            phase=UiPathRuntimeStatePhase.COMPLETED,
        )

    # ALWAYS yield final result
    yield UiPathRuntimeResult(
        output=serialize_json(final_output),
        status=UiPathRuntimeStatus.SUCCESSFUL,
    )
```

**Note:** If your framework doesn't expose node-level execution hooks, you can still
support breakpoints by mapping framework events to nodes. The `UiPathDebugRuntime`
wrapper will intercept `UiPathBreakpointResult` events and handle the pause/resume cycle.

#### Streaming Strategies by Framework Type

**A) Framework has async generator / async iterator:**
```python
# Direct mapping — cleanest approach
async for event in framework.astream(input):
    yield map_to_uipath_event(event)
yield final_result
```

**B) Framework has callback/event hooks:**
```python
# Use asyncio.Queue as bridge
queue: asyncio.Queue[UiPathRuntimeEvent] = asyncio.Queue()

def on_node_start(node_name, data):
    queue.put_nowait(UiPathRuntimeStateEvent(
        payload=data, node_name=node_name,
        phase=UiPathRuntimeStatePhase.STARTED,
    ))

# Register callbacks, run agent, drain queue
task = asyncio.create_task(self._run_with_callbacks(input, queue))
while not task.done() or not queue.empty():
    event = await asyncio.wait_for(queue.get(), timeout=1.0)
    yield event
```

**C) Framework is synchronous only (MINIMAL tier):**
```python
# Wrap execute in stream
async def stream(self, input, options):
    result = await self.execute(input, UiPathExecuteOptions(**options.model_dump()))
    yield result
```

#### If Streaming Is Not Supported

If the framework truly cannot stream, raise the dedicated exception:

```python
from uipath.runtime import UiPathStreamNotSupportedError

async def stream(self, input, options):
    raise UiPathStreamNotSupportedError()
```

#### QUALITY GATE 5
```
✓ stream() yields UiPathRuntimeStateEvent with STARTED/COMPLETED phases for each node
✓ node_name values in state events match node IDs in the graph from get_schema()
✓ stream() always yields UiPathRuntimeResult as the final event
✓ Breakpoints are checked when options.breakpoints is set
✓ UiPathBreakpointResult includes breakpoint_node, current_state, next_nodes
✓ Message events are yielded for LLM output (text chunks, tool calls)
```

---

### Step 6: HITL / Resumability

> Implement suspend/resume for human-in-the-loop scenarios.

**Skip this step if capability tier is not FULL.**

When an agent needs human input (e.g., approval, clarification), execution suspends:
1. Your runtime detects the interrupt signal from the framework
2. You yield a `UiPathRuntimeResult` with `status=SUSPENDED` and a `UiPathResumeTrigger`
3. The `UiPathResumableRuntime` wrapper persists the trigger to storage
4. Later, when human input arrives, the CLI calls `execute(input, resume=True)`
5. Your runtime loads the checkpoint and resumes

#### Storage Implementation

```python
"""SQLite storage adapter for <Framework> HITL state."""

from __future__ import annotations

import json
from pathlib import Path

import aiosqlite

from uipath.runtime import UiPathResumableStorageProtocol, UiPathResumeTrigger


class <Framework>SqliteStorage:
    """SQLite-backed storage for resume triggers and key-value pairs."""

    def __init__(self, db_path: str = ".uipath/state.db") -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def setup(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS __uipath_resume_triggers "
            "(runtime_id TEXT, data TEXT)"
        )
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS __uipath_runtime_kv "
            "(runtime_id TEXT, namespace TEXT, key TEXT, value TEXT, "
            "PRIMARY KEY (runtime_id, namespace, key))"
        )
        await self._db.commit()

    async def save_triggers(
        self, runtime_id: str, triggers: list[UiPathResumeTrigger]
    ) -> None:
        assert self._db
        for trigger in triggers:
            await self._db.execute(
                "INSERT INTO __uipath_resume_triggers VALUES (?, ?)",
                (runtime_id, trigger.model_dump_json()),
            )
        await self._db.commit()

    async def get_triggers(
        self, runtime_id: str
    ) -> list[UiPathResumeTrigger] | None:
        assert self._db
        cursor = await self._db.execute(
            "SELECT data FROM __uipath_resume_triggers WHERE runtime_id = ?",
            (runtime_id,),
        )
        rows = await cursor.fetchall()
        if not rows:
            return None
        return [UiPathResumeTrigger.model_validate_json(row[0]) for row in rows]

    async def delete_trigger(
        self, runtime_id: str, trigger: UiPathResumeTrigger
    ) -> None:
        assert self._db
        await self._db.execute(
            "DELETE FROM __uipath_resume_triggers "
            "WHERE runtime_id = ? AND data = ?",
            (runtime_id, trigger.model_dump_json()),
        )
        await self._db.commit()

    async def set_value(
        self, runtime_id: str, namespace: str, key: str, value: Any
    ) -> None:
        assert self._db
        await self._db.execute(
            "INSERT OR REPLACE INTO __uipath_runtime_kv VALUES (?, ?, ?, ?)",
            (runtime_id, namespace, key, json.dumps(value)),
        )
        await self._db.commit()

    async def get_value(
        self, runtime_id: str, namespace: str, key: str
    ) -> Any:
        assert self._db
        cursor = await self._db.execute(
            "SELECT value FROM __uipath_runtime_kv "
            "WHERE runtime_id = ? AND namespace = ? AND key = ?",
            (runtime_id, namespace, key),
        )
        row = await cursor.fetchone()
        return json.loads(row[0]) if row else None

    async def dispose(self) -> None:
        if self._db:
            await self._db.close()
```

#### Suspension Pattern in stream()

When the framework signals an interrupt:

```python
from uipath.runtime import (
    UiPathRuntimeResult,
    UiPathRuntimeStatus,
    UiPathResumeTrigger,
    UiPathResumeTriggerType,
    UiPathResumeTriggerName,
    UiPathApiTrigger,
)


# Inside stream():
if framework_signals_interrupt(event):
    trigger = UiPathResumeTrigger(
        interrupt_id=str(event.interrupt_id),
        trigger_type=UiPathResumeTriggerType.API,
        trigger_name=UiPathResumeTriggerName(
            name=event.tool_name or "human_input",
            title=event.prompt or "Waiting for input",
        ),
        api_resume=UiPathApiTrigger(
            payload=event.context,  # Data needed to resume
        ),
    )

    yield UiPathRuntimeResult(
        output=None,
        status=UiPathRuntimeStatus.SUSPENDED,
        trigger=trigger,         # Single trigger
        # OR triggers=[trigger]  # Multiple concurrent triggers
    )
    return  # Stop streaming — wrapper handles persistence
```

#### Wrapping with UiPathResumableRuntime

In your factory (Step 7), wrap the base runtime:

```python
from uipath.runtime import UiPathResumableRuntime, UiPathResumeTriggerProtocol

# Your trigger handler (usually UiPathResumeTriggerHandler from uipath SDK)
from uipath._services._resume_trigger_handler import UiPathResumeTriggerHandler

runtime = UiPath<Framework>Runtime(agent, runtime_id, entrypoint)
storage = <Framework>SqliteStorage()
await storage.setup()

resumable = UiPathResumableRuntime(
    delegate=runtime,
    storage=storage,
    trigger_manager=UiPathResumeTriggerHandler(),
    runtime_id=runtime_id,
)
return resumable  # Return this from factory.new_runtime()
```

#### QUALITY GATE 6
```
✓ Storage correctly persists and retrieves triggers via SQLite
✓ Suspension yields UiPathRuntimeResult with SUSPENDED status
✓ Resume trigger includes interrupt_id, trigger_type, trigger_name
✓ UiPathResumableRuntime wraps the base runtime in factory
✓ Resume with options.resume=True correctly resumes from checkpoint
```

---

### Step 7: Factory & Registration

> Implement the factory that creates runtime instances and register it.

#### Factory Implementation

```python
"""Runtime factory for <Framework> integration."""

from __future__ import annotations

import asyncio
from typing import Any

from uipath.runtime import (
    UiPathRuntimeContext,
    UiPathRuntimeFactoryProtocol,
    UiPathRuntimeFactoryRegistry,
    UiPathRuntimeFactorySettings,
    UiPathRuntimeProtocol,
    UiPathRuntimeStorageProtocol,
    UiPathResumableRuntime,
)

from .config import <Framework>Config
from .loader import <Framework>AgentLoader
from .runtime import UiPath<Framework>Runtime
from .errors import UiPath<Framework>ErrorCode, UiPath<Framework>RuntimeError


class UiPath<Framework>RuntimeFactory:
    """Factory for creating <Framework> runtime instances."""

    def __init__(self, context: UiPathRuntimeContext) -> None:
        self._context = context
        self._config = <Framework>Config()
        self._agent_cache: dict[str, Any] = {}
        self._agent_lock = asyncio.Lock()
        self._storage: <Framework>SqliteStorage | None = None  # FULL tier only

    def discover_entrypoints(self) -> list[str]:
        """List available agents from config file."""
        return list(self._config.agents.keys())

    async def new_runtime(
        self,
        entrypoint: str,
        runtime_id: str,
        **kwargs: Any,
    ) -> UiPathRuntimeProtocol:
        """Create a new runtime instance for the given entrypoint."""
        if entrypoint not in self._config.agents:
            raise UiPath<Framework>RuntimeError(
                code=UiPath<Framework>ErrorCode.CONFIG_INVALID,
                title=f"Unknown entrypoint: {entrypoint}",
                detail=f"Available: {list(self._config.agents.keys())}",
                category=UiPathErrorCategory.USER,
            )

        agent = await self._resolve_agent(entrypoint)

        runtime = UiPath<Framework>Runtime(
            agent=agent,
            runtime_id=runtime_id,
            entrypoint=entrypoint,
        )

        # FULL tier: Wrap with UiPathResumableRuntime
        # CORE/MINIMAL: Return runtime directly
        #
        # if self._storage:
        #     from uipath._services._resume_trigger_handler import (
        #         UiPathResumeTriggerHandler,
        #     )
        #     return UiPathResumableRuntime(
        #         delegate=runtime,
        #         storage=self._storage,
        #         trigger_manager=UiPathResumeTriggerHandler(),
        #         runtime_id=runtime_id,
        #     )

        return runtime

    async def _resolve_agent(self, entrypoint: str) -> Any:
        """Load and optionally cache the agent for the given entrypoint.

        IMPORTANT: Only cache agents if the framework allows the same instance
        to be executed concurrently. Some frameworks (e.g., Agent Framework)
        maintain internal state per execution, so each runtime must get a fresh
        agent instance. When in doubt, don't cache.

        Cache-safe:  OpenAI Agents, Google ADK, LangGraph (compiled graphs are reusable)
        NOT safe:    Agent Framework (workflow instances hold execution state)
        """
        # Option A: With caching (if framework supports concurrent reuse)
        async with self._agent_lock:
            if entrypoint in self._agent_cache:
                return self._agent_cache[entrypoint]

            entrypoint_path = self._config.agents[entrypoint]
            loader = <Framework>AgentLoader.from_entrypoint(entrypoint_path)
            agent = await loader.load()

            self._agent_cache[entrypoint] = agent
            return agent

        # Option B: Without caching (if framework doesn't allow concurrent reuse)
        # entrypoint_path = self._config.agents[entrypoint]
        # loader = <Framework>AgentLoader.from_entrypoint(entrypoint_path)
        # return await loader.load()

    async def get_storage(self) -> UiPathRuntimeStorageProtocol | None:
        """Return storage for HITL state (FULL tier only)."""
        return self._storage

    async def get_settings(self) -> UiPathRuntimeFactorySettings | None:
        """Return factory settings."""
        return None

    async def dispose(self) -> None:
        """Cleanup resources."""
        if self._storage:
            await self._storage.dispose()
        self._agent_cache.clear()
```

#### Registration Function

This is what the entry point in `pyproject.toml` calls:

```python
# At module level in runtime/__init__.py or runtime/factory.py

def register_runtime_factory() -> None:
    """Register the <Framework> runtime factory."""

    def factory_callable(
        context: UiPathRuntimeContext,
    ) -> UiPath<Framework>RuntimeFactory:
        return UiPath<Framework>RuntimeFactory(context)

    UiPathRuntimeFactoryRegistry.register(
        "<framework-id>",            # Unique factory ID
        factory_callable,            # Creates factory from context
        "<framework>.json",          # Config file that triggers this factory
    )
```

#### How Discovery Works (CLI Flow)

When a user runs `uipath init` or `uipath run`:

1. CLI calls `UiPathRuntimeFactoryRegistry.get(context=ctx)`
2. Registry iterates over all registered factories (via entry points)
3. For each factory, checks if its config file exists in the current directory
4. Returns the factory whose config file is found
5. CLI calls `factory.discover_entrypoints()` to list available agents
6. CLI calls `factory.new_runtime(entrypoint, runtime_id)` to create runtime

During `uipath init`, the CLI also:
1. Calls `runtime.get_schema()` for each entrypoint
2. Writes `entry-points.json` with all schemas (input, output, graph)
3. Generates mermaid diagrams from the graph
4. Generates AGENTS.md documentation

#### QUALITY GATE 7
```
✓ Factory discovers entrypoints from config file
✓ new_runtime() returns a valid UiPathRuntimeProtocol implementation
✓ Registration function correctly registers with UiPathRuntimeFactoryRegistry
✓ Agent caching with asyncio.Lock IF the framework allows concurrent reuse of the same instance
  (e.g., OpenAI Agents, Google ADK, LangGraph — compiled graphs/agents are stateless)
✓ NO caching if the framework maintains internal execution state per instance
  (e.g., Agent Framework — workflow instances cannot be reused concurrently)
✓ Unknown entrypoint raises proper error
✓ dispose() cleans up all resources
```

---

### Step 8: LLM Provider Classes (UiPath LLM Gateway)

> Route LLM calls through UiPath's LLM Gateway for managed access.

When agents run on the UiPath platform, LLM calls should route through the UiPath LLM
Gateway (AgentHub). This provides centralized model management, rate limiting, and billing.

#### Gateway Architecture

```
Your Agent → Framework LLM Client → HTTP Transport (intercepted) → UiPath Gateway → LLM Provider
                                          ↑
                                    URL rewriting +
                                    auth injection
```

**Gateway URL format:**
```
{UIPATH_URL}/agenthub_/llm/raw/vendor/{vendor}/model/{model}/completions
```

**Vendor mapping:**

| Model Prefix | Vendor | API Flavor Header |
|-------------|--------|-------------------|
| `gpt-*`, `o1-*`, `o3-*` | `openai` | `OpenAiChatCompletions` |
| `gemini-*` | `vertexai` | `GeminiGenerateContent` |
| `anthropic.*` (Bedrock names) | `awsbedrock` | `invoke` or `converse` |
| `claude-*` (direct) | `vertexai` | `AnthropicClaude` |

#### Required Headers

Every request to the gateway MUST include:

| Header | Value | Required |
|--------|-------|----------|
| `Authorization` | `Bearer {UIPATH_ACCESS_TOKEN}` | YES |
| `X-UiPath-LlmGateway-ApiFlavor` | See vendor mapping | YES |
| `X-UiPath-Streaming-Enabled` | `"true"` or `"false"` | YES |
| `X-UiPath-JobKey` | `{UIPATH_JOB_KEY}` | Optional |
| `X-UiPath-ProcessKey` | `{UIPATH_PROCESS_KEY}` | Optional |
| `X-UiPath-AgentHub-Config` | Config name | Optional |
| `X-UiPath-LlmGateway-ByoIsConnectionId` | Connection ID | Optional |

**Without `X-UiPath-LlmGateway-ApiFlavor`, the gateway returns HTTP 500.**

#### Environment Variables

```python
import os

UIPATH_URL = os.environ.get("UIPATH_URL", "")
UIPATH_ACCESS_TOKEN = os.environ.get("UIPATH_ACCESS_TOKEN", "")
UIPATH_JOB_KEY = os.environ.get("UIPATH_JOB_KEY")
UIPATH_PROCESS_KEY = os.environ.get("UIPATH_PROCESS_KEY")
```

#### Implementation Strategy: URL-Rewriting Transport

The cleanest approach is to intercept HTTP at the transport layer, rewriting URLs and
injecting headers. This works with any SDK that uses `httpx` internally.

```python
"""UiPath LLM Gateway transport for <Framework>."""

from __future__ import annotations

import os
from urllib.parse import quote

import httpx
from uipath._utils._ssl_context import get_httpx_client_kwargs


def _get_uipath_headers(streaming: bool = False) -> dict[str, str]:
    """Build gateway headers."""
    token = os.environ.get("UIPATH_ACCESS_TOKEN", "")
    headers = {
        "Authorization": f"Bearer {token}",
        "X-UiPath-Streaming-Enabled": "true" if streaming else "false",
    }
    job_key = os.environ.get("UIPATH_JOB_KEY")
    if job_key:
        headers["X-UiPath-JobKey"] = job_key
    process_key = os.environ.get("UIPATH_PROCESS_KEY")
    if process_key:
        headers["X-UiPath-ProcessKey"] = process_key
    return headers


def _build_gateway_url(vendor: str, model: str) -> str:
    """Build the gateway URL for a given vendor and model."""
    base_url = os.environ.get("UIPATH_URL", "")
    encoded_model = quote(model, safe="")
    return f"{base_url}/agenthub_/llm/raw/vendor/{vendor}/model/{encoded_model}/completions"


class _AsyncUrlRewriteTransport(httpx.AsyncBaseTransport):
    """Rewrites outgoing HTTP requests to route through UiPath Gateway."""

    def __init__(
        self,
        gateway_url: str,
        api_flavor: str,
        wrapped: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._gateway_url = gateway_url
        self._api_flavor = api_flavor
        self._wrapped = wrapped or httpx.AsyncHTTPTransport(
            **get_httpx_client_kwargs()
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        # Detect if streaming from request body
        streaming = self._is_streaming(request)

        # Rewrite URL
        request.url = httpx.URL(self._gateway_url)

        # Inject headers
        headers = _get_uipath_headers(streaming=streaming)
        headers["X-UiPath-LlmGateway-ApiFlavor"] = self._api_flavor
        for key, value in headers.items():
            request.headers[key] = value

        return await self._wrapped.handle_async_request(request)

    def _is_streaming(self, request: httpx.Request) -> bool:
        """Detect streaming from request body."""
        # Read body and check for "stream": true
        try:
            import json
            body = json.loads(request.content)
            return body.get("stream", False)
        except Exception:
            return False


class _SyncUrlRewriteTransport(httpx.BaseTransport):
    """Sync version of URL rewrite transport."""

    def __init__(
        self,
        gateway_url: str,
        api_flavor: str,
        wrapped: httpx.BaseTransport | None = None,
    ) -> None:
        self._gateway_url = gateway_url
        self._api_flavor = api_flavor
        self._wrapped = wrapped or httpx.HTTPTransport(
            **get_httpx_client_kwargs()
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        streaming = False
        try:
            import json
            body = json.loads(request.content)
            streaming = body.get("stream", False)
        except Exception:
            pass

        request.url = httpx.URL(self._gateway_url)
        headers = _get_uipath_headers(streaming=streaming)
        headers["X-UiPath-LlmGateway-ApiFlavor"] = self._api_flavor
        for key, value in headers.items():
            request.headers[key] = value

        return self._wrapped.handle_request(request)
```

#### Per-Provider Integration

**OpenAI models** (most common):
```python
"""UiPath OpenAI chat model for <Framework>."""

from __future__ import annotations

from functools import cached_property

# Import from your framework's OpenAI integration
from <framework>.llms import OpenAIChat  # or whatever the class is


class UiPathOpenAI(OpenAIChat):
    """OpenAI chat model routed through UiPath Gateway."""

    model: str = "gpt-4.1-2025-04-14"

    @cached_property
    def _client(self):
        """Override SDK client to inject gateway transport."""
        import openai

        gateway_url = _build_gateway_url("openai", self.model)
        transport = _AsyncUrlRewriteTransport(
            gateway_url=gateway_url,
            api_flavor="OpenAiChatCompletions",
        )
        http_client = httpx.AsyncClient(transport=transport)
        return openai.AsyncOpenAI(
            api_key="gateway-managed",  # Not used — gateway handles auth
            http_client=http_client,
        )
```

**Anthropic models:**
```python
class UiPathAnthropic(FrameworkAnthropicBase):
    """Anthropic model routed through UiPath Gateway (via AWS Bedrock)."""

    @cached_property
    def _client(self):
        import anthropic

        gateway_url = _build_gateway_url("awsbedrock", self.model)
        transport = _AsyncUrlRewriteTransport(
            gateway_url=gateway_url,
            api_flavor="invoke",
        )
        http_client = httpx.AsyncClient(transport=transport)
        return anthropic.AsyncAnthropic(
            api_key="gateway-managed",
            http_client=http_client,
        )
```

**Gemini models:**
```python
class UiPathGemini(FrameworkGeminiBase):
    """Gemini model routed through UiPath Gateway (via Vertex AI)."""

    @cached_property
    def api_client(self):
        from google import genai
        from google.genai import types

        gateway_url = _build_gateway_url("vertexai", self.model)
        transport = _AsyncUrlRewriteTransport(
            gateway_url=gateway_url,
            api_flavor="GeminiGenerateContent",
        )
        http_client = httpx.AsyncClient(transport=transport)
        return genai.Client(
            api_key="gateway-managed",
            http_options=types.HttpOptions(
                httpx_async_client=http_client,
            ),
        )
```

#### SSL Context

Always use UiPath's SSL context helper for proper certificate handling:

```python
from uipath._utils._ssl_context import get_httpx_client_kwargs

# Use in transport creation:
transport = httpx.AsyncHTTPTransport(**get_httpx_client_kwargs())
```

#### Lazy Imports for Optional Dependencies

LLM providers are optional — not every project needs all providers. Use lazy imports:

```python
# In chat/__init__.py
def __getattr__(name: str):
    if name == "UiPathOpenAI":
        from .openai import UiPathOpenAI
        return UiPathOpenAI
    if name == "UiPathAnthropic":
        from .anthropic import UiPathAnthropic
        return UiPathAnthropic
    if name == "UiPathGemini":
        from .gemini import UiPathGemini
        return UiPathGemini
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
```

#### QUALITY GATE 8
```
✓ Gateway URL is correctly constructed with vendor and model
✓ X-UiPath-LlmGateway-ApiFlavor header is set (without it → 500)
✓ Authorization header includes Bearer token
✓ Streaming detection works (X-UiPath-Streaming-Enabled)
✓ SSL context uses get_httpx_client_kwargs()
✓ Provider classes are lazily imported
✓ API key is set to placeholder (gateway handles actual auth)
```

---

### Step 9: CLI Middleware & Scaffolding

> Register CLI commands so `uipath new` can scaffold projects for your framework.

#### middlewares.py

```python
"""CLI middleware registration for <Framework> integration."""

from __future__ import annotations

from uipath._cli._middlewares import Middlewares


def register_middleware() -> None:
    """Register the <framework> new-project middleware."""
    Middlewares.register("new", _<framework>_new_middleware)


def _<framework>_new_middleware(next_handler, **kwargs):
    """Middleware for 'uipath new' command — offers <Framework> as an option."""
    from ._cli.cli_new import <framework>_new
    return <framework>_new(next_handler, **kwargs)
```

#### cli_new.py — Scaffolding Command

```python
"""Scaffolding for new <Framework> projects."""

from __future__ import annotations

import shutil
from pathlib import Path


def <framework>_new(next_handler, **kwargs):
    """Create a new <Framework> project from template."""
    template_dir = Path(__file__).parent / "_templates"

    # Copy template files
    shutil.copy(template_dir / "main.py.template", "main.py")
    shutil.copy(template_dir / "config.json.template", "<framework>.json")

    # Generate pyproject.toml if not exists
    if not Path("pyproject.toml").exists():
        _generate_pyproject()

    print(f"Created <Framework> project. Next steps:")
    print(f"  1. Edit main.py to define your agent")
    print(f"  2. Run: uipath init")
    print(f"  3. Run: uipath run <entrypoint>")
```

#### Templates

**_templates/main.py.template:**
```python
"""<Framework> agent definition."""

from <framework> import Agent  # Adjust import for your framework


# Define your agent here
agent = Agent(
    name="my_agent",
    instructions="You are a helpful assistant.",
    # tools=[...],
)
```

**_templates/config.json.template:**
```json
{
  "agents": {
    "agent": "main.py:agent"
  }
}
```

#### QUALITY GATE 9
```
✓ `uipath new` shows the framework as an option
✓ Template files are created in the current directory
✓ Generated main.py is valid Python (no syntax errors)
✓ Generated config JSON is valid and references main.py:agent
✓ `uipath init` successfully discovers the generated entrypoint
```

---

## Phase 3 — Integration Polish

### Tests

#### conftest.py

```python
"""Test fixtures for <Framework> integration."""

import pytest
from click.testing import CliRunner


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def sample_config(tmp_path):
    """Create a sample config file."""
    config = tmp_path / "<framework>.json"
    config.write_text('{"agents": {"test_agent": "main.py:agent"}}')
    return config
```

#### test_schema.py

```python
"""Tests for schema inference."""

import pytest
from uipath_<framework>.runtime.schema import build_graph


def test_graph_has_unique_node_ids():
    """Node IDs must be unique — they're breakpoint targets."""
    graph = build_graph(mock_agent)
    ids = [node.id for node in graph.nodes]
    assert len(ids) == len(set(ids))


def test_schema_has_input_output():
    """Schema must have valid input and output JSON schemas."""
    schema = runtime.get_schema()
    assert "type" in schema.input
    assert "type" in schema.output
```

#### test_runtime.py

```python
"""Tests for runtime execution."""

import pytest
from uipath.runtime import UiPathExecuteOptions, UiPathRuntimeStatus


@pytest.mark.asyncio
async def test_execute_returns_successful():
    result = await runtime.execute({"messages": "hello"}, UiPathExecuteOptions())
    assert result.status == UiPathRuntimeStatus.SUCCESSFUL
    assert result.output is not None


@pytest.mark.asyncio
async def test_stream_yields_state_events():
    from uipath.runtime.events import UiPathRuntimeStateEvent, UiPathRuntimeStatePhase

    events = []
    async for event in runtime.stream({"messages": "hello"}, UiPathStreamOptions()):
        events.append(event)

    state_events = [e for e in events if isinstance(e, UiPathRuntimeStateEvent)]
    assert any(e.phase == UiPathRuntimeStatePhase.STARTED for e in state_events)
    assert any(e.phase == UiPathRuntimeStatePhase.COMPLETED for e in state_events)
```

### README.md Template

```markdown
# UiPath <Framework> Integration

UiPath runtime integration for [<Framework>](framework-url).

## Installation

```bash
pip install uipath-<framework>
```

## Quick Start

1. Create your agent in `main.py`
2. Create `<framework>.json` with entrypoint mapping
3. Run `uipath init` to generate schemas
4. Run `uipath run <entrypoint>` to execute

## Configuration

`<framework>.json`:
```json
{
  "agents": {
    "agent": "main.py:agent"
  }
}
```

## Features

| Feature | Supported |
|---------|-----------|
| Execute | Yes |
| Streaming | Yes/No |
| HITL | Yes/No |
| Breakpoints | Yes/No |
| LLM Gateway | Yes/No |
```

---

## Quick Reference — All Key Imports

```python
# Core protocols
from uipath.runtime import (
    UiPathRuntimeProtocol,
    UiPathRuntimeFactoryProtocol,
    UiPathRuntimeFactoryRegistry,
    UiPathRuntimeFactorySettings,
    UiPathRuntimeContext,
)

# Execution
from uipath.runtime import (
    UiPathExecuteOptions,
    UiPathStreamOptions,
    UiPathRuntimeResult,
    UiPathRuntimeStatus,         # SUCCESSFUL, FAULTED, SUSPENDED
    UiPathStreamNotSupportedError,
)

# Events
from uipath.runtime import UiPathRuntimeEvent
from uipath.runtime.events import (
    UiPathRuntimeEventType,      # RUNTIME_MESSAGE, RUNTIME_STATE, RUNTIME_ERROR, RUNTIME_RESULT
    UiPathRuntimeStatePhase,     # STARTED, UPDATED, COMPLETED, FAULTED
    UiPathRuntimeMessageEvent,
    UiPathRuntimeStateEvent,
)

# Schema & Graph
from uipath.runtime import UiPathRuntimeSchema
from uipath.runtime.schema import (
    UiPathRuntimeGraph,
    UiPathRuntimeNode,
    UiPathRuntimeEdge,
    transform_references,
    transform_nullable_types,
    transform_attachments,
)

# HITL / Resumable
from uipath.runtime import (
    UiPathResumableRuntime,
    UiPathResumableStorageProtocol,
    UiPathResumeTriggerProtocol,
    UiPathResumeTrigger,
    UiPathResumeTriggerType,     # API, JOB, TASK, QUEUE_ITEM, ...
    UiPathResumeTriggerName,
    UiPathApiTrigger,
    UiPathRuntimeStorageProtocol,
)

# Debug (you don't implement these — the CLI applies them)
from uipath.runtime import (
    UiPathDebugProtocol,
    UiPathDebugRuntime,
    UiPathBreakpointResult,
    UiPathDebugQuitError,
)

# Chat (you don't implement this — the CLI applies it)
from uipath.runtime import (
    UiPathChatProtocol,
    UiPathChatRuntime,
)

# Execution wrapper (you don't implement this — the CLI applies it)
from uipath.runtime import UiPathExecutionRuntime

# Serialization
from uipath.core.serialization import serialize_json

# Errors
from uipath.runtime.errors import (
    UiPathBaseRuntimeError,
    UiPathErrorContract,
    UiPathErrorCategory,         # USER, SYSTEM, DEPLOYMENT, UNKNOWN
    UiPathErrorCode,
)

# SSL (for LLM Gateway)
from uipath._utils._ssl_context import get_httpx_client_kwargs
```

---

## Checklist — Integration Completeness

### Required (all tiers)
- [ ] `pyproject.toml` with entry points for `uipath.runtime.factories` and `uipath.middlewares`
- [ ] Config file parser (`<framework>.json`)
- [ ] Agent loader (dynamic import from `file.py:variable`)
- [ ] Error codes and exception class
- [ ] `get_schema()` returning `UiPathRuntimeSchema` with input/output schemas
- [ ] `execute()` returning `UiPathRuntimeResult`
- [ ] Factory with `discover_entrypoints()` and `new_runtime()`
- [ ] Registration function for `UiPathRuntimeFactoryRegistry`
- [ ] CLI middleware for `uipath new`

### Required for CORE+ tier
- [ ] `stream()` yielding `UiPathRuntimeEvent` objects
- [ ] `UiPathRuntimeStateEvent` with STARTED/COMPLETED phases per node
- [ ] `node_name` in state events matches `UiPathRuntimeNode.id` in graph
- [ ] `UiPathRuntimeMessageEvent` for LLM output
- [ ] Final event is always `UiPathRuntimeResult`
- [ ] Graph building with nodes and edges for visualization

### Required for FULL tier
- [ ] SQLite storage for resume triggers
- [ ] Suspension detection (framework interrupts → SUSPENDED status)
- [ ] `UiPathResumeTrigger` creation with proper trigger types
- [ ] `UiPathResumableRuntime` wrapping in factory
- [ ] Resume support (`options.resume=True`)

### Optional (recommended)
- [ ] Breakpoint support (`UiPathBreakpointResult` for node-level debugging)
- [ ] LLM provider classes routing through UiPath Gateway
- [ ] `qualified_node_name` for subgraph hierarchy in state events
- [ ] Agent caching with `asyncio.Lock` in factory (only if framework allows concurrent reuse)
- [ ] Lazy imports for optional dependencies
- [ ] Sample project in `samples/`
- [ ] Integration tests

---

## Reference: Existing Integrations

Study these for real-world patterns:

| Integration | Package | Config File | Streaming | HITL | LLM Gateway |
|-------------|---------|-------------|-----------|------|-------------|
| **LangGraph** | `uipath-langchain-python` | `langgraph.json` | Yes (async gen) | Yes | Yes (OpenAI) |
| **OpenAI Agents** | `uipath-openai-agents` | `openai_agents.json` | Yes (async gen) | No | Yes (OpenAI) |
| **LlamaIndex** | `uipath-llamaindex` | `llama_index.json` | Yes (async gen) | Yes | Yes (Bedrock, Vertex) |
| **Google ADK** | `uipath-google-adk` | `google_adk.json` | Yes (async gen) | Partial | Yes (OpenAI, Anthropic, Gemini) |
| **Agent Framework** | `uipath-agent-framework` | `agent_framework.json` | Yes (async gen) | Yes | No |
