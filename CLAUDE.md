# UiPath Runtime Python SDK

This repository contains the runtime abstractions and interfaces for building agent
integrations in the UiPath ecosystem.

## Building a New Integration

If you are here to build a new integration for a Python agentic framework, read and
follow **INTEGRATION_GENOME.md** — it is a complete blueprint that guides you through
every step of building a UiPath runtime integration.

Start by reading the genome's Phase 0 (Framework Discovery) and asking the user the
discovery questions. Then proceed through each phase sequentially, following the quality
gates.

## Project Structure

- `src/uipath/runtime/` — Core protocols and contracts
- `src/uipath/runtime/base.py` — `UiPathRuntimeProtocol`, `UiPathExecutionRuntime`
- `src/uipath/runtime/context.py` — `UiPathRuntimeContext` (execution context, file I/O, log capture)
- `src/uipath/runtime/result.py` — `UiPathRuntimeResult`, `UiPathRuntimeStatus`
- `src/uipath/runtime/factory.py` — `UiPathRuntimeFactoryProtocol`
- `src/uipath/runtime/registry.py` — `UiPathRuntimeFactoryRegistry`
- `src/uipath/runtime/schema.py` — Schema models (`UiPathRuntimeGraph`, nodes, edges)
- `src/uipath/runtime/storage.py` — `UiPathRuntimeStorageProtocol` (key-value storage)
- `src/uipath/runtime/events/` — Event types (`UiPathRuntimeStateEvent`, `UiPathRuntimeMessageEvent`)
- `src/uipath/runtime/errors/` — Error contracts and categories
- `src/uipath/runtime/resumable/` — HITL support (`UiPathResumableRuntime`)
- `src/uipath/runtime/debug/` — Debugger support (`UiPathDebugRuntime`, breakpoints)
- `src/uipath/runtime/chat/` — Chat bridge support (`UiPathChatRuntime`)
- `src/uipath/runtime/logging/` — Log interception, file handlers, context-aware filters
- `INTEGRATION_GENOME.md` — Complete guide for building new integrations

## Development

```bash
uv sync --all-extras
uv run ruff check .
uv run ruff check --fix .
uv run pytest
```

## Reference Integrations

| Package | Repository | Tier |
|---------|-----------|------|
| `uipath-langchain` | `uipath-langchain-python` | FULL (streaming, HITL, LLM Gateway) |
| `uipath-openai-agents` | `uipath-integrations-python` | CORE (streaming, LLM Gateway) |
| `uipath-llamaindex` | `uipath-integrations-python` | FULL (streaming, HITL, LLM Gateway) |
| `uipath-google-adk` | `uipath-integrations-python` | CORE (streaming, LLM Gateway) |
| `uipath-agent-framework` | `uipath-integrations-python` | FULL (streaming, HITL) |
| `uipath-mcp` | `uipath-mcp-python` | MCP integration |
