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
- `src/uipath/runtime/factory.py` — `UiPathRuntimeFactoryProtocol`
- `src/uipath/runtime/registry.py` — `UiPathRuntimeFactoryRegistry`
- `src/uipath/runtime/events/` — Event types (`UiPathRuntimeStateEvent`, `UiPathRuntimeMessageEvent`)
- `src/uipath/runtime/resumable/` — HITL support (`UiPathResumableRuntime`)
- `src/uipath/runtime/debug/` — Debugger support (`UiPathDebugRuntime`, breakpoints)
- `src/uipath/runtime/chat/` — Chat bridge support (`UiPathChatRuntime`)
- `src/uipath/runtime/schema.py` — Schema models (`UiPathRuntimeGraph`, nodes, edges)
- `src/uipath/runtime/errors/` — Error contracts and categories
- `INTEGRATION_GENOME.md` — Complete guide for building new integrations

## Development

```bash
uv sync --all-extras
uv run ruff check .
uv run ruff check --fix .
uv run pytest
```

## Reference Integrations

- `uipath-langchain-python` — LangGraph integration (FULL tier)
- `uipath-integrations-python` — OpenAI Agents, LlamaIndex, Google ADK, Agent Framework
