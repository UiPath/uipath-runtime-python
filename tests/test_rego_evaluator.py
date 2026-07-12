"""Tests for RegoEvaluator: enforcement modes, audit emission, WASM mock."""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from uipath.core.governance import EnforcementMode
from uipath.core.governance.exceptions import GovernanceBlockException
from uipath.core.governance.models import Action, LifecycleHook

from uipath.runtime.governance.rego.evaluator import (
    RegoEvaluator,
    _extract_data_json_from_bundle,
    _extract_wasm_from_bundle,
    _pack_name_from_rule_id,
    context_to_input,
)
from uipath.runtime.governance.native.models import CheckContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

import io
import json
import tarfile


def _make_bundle(wasm: bytes = b"\x00asm", data: dict | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in [("policy.wasm", wasm)] + (
            [("data.json", json.dumps(data).encode())] if data else []
        ):
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _write_bundle(path: Path, **kwargs: Any) -> Path:
    path.write_bytes(_make_bundle(**kwargs))
    return path


def _make_context(
    hook: LifecycleHook = LifecycleHook.BEFORE_MODEL,
    agent_name: str = "test-agent",
    runtime_id: str = "run-1",
    model_input: str = "hello",
) -> CheckContext:
    return CheckContext(
        hook=hook,
        agent_name=agent_name,
        runtime_id=runtime_id,
        model_input=model_input,
    )


def _make_engine(result: dict | None = None) -> MagicMock:
    """Return a fake OPA engine whose evaluate() returns a single-item list."""
    engine = MagicMock()
    engine.evaluate.return_value = [{"result": result or {"deny": False}}]
    return engine


def _make_evaluator(
    hook: LifecycleHook = LifecycleHook.BEFORE_MODEL,
    engine_result: dict | None = None,
    enforcement_mode: EnforcementMode = EnforcementMode.ENFORCE,
    bundle_path: Path | None = None,
) -> tuple[RegoEvaluator, MagicMock]:
    engine = _make_engine(engine_result)
    with patch("uipath.runtime.governance.rego.evaluator._load_engine", return_value=engine):
        ev = RegoEvaluator(
            hook_wasm_paths={hook: bundle_path or Path("/fake/bundle.tar.gz")},
            enforcement_mode=enforcement_mode,
        )
    return ev, engine


# ---------------------------------------------------------------------------
# _pack_name_from_rule_id
# ---------------------------------------------------------------------------

def test_pack_name_with_slash() -> None:
    assert _pack_name_from_rule_id("my_pack/block_ssn") == "my_pack"


def test_pack_name_without_slash() -> None:
    assert _pack_name_from_rule_id("block_ssn") == "custom"


# ---------------------------------------------------------------------------
# context_to_input
# ---------------------------------------------------------------------------

def test_context_to_input_basic_fields() -> None:
    ctx = _make_context(hook=LifecycleHook.BEFORE_MODEL, model_input="test input")
    result = context_to_input(ctx)
    assert result["hook"] == "before_model"
    assert result["model_input"] == "test input"
    assert result["agent_name"] == "test-agent"


def test_context_to_input_session_state_defaults() -> None:
    ctx = _make_context()
    result = context_to_input(ctx)
    assert result["session_state"] == {"tool_calls": 0, "llm_calls": 0}


# ---------------------------------------------------------------------------
# _extract_wasm_from_bundle / _extract_data_json_from_bundle
# ---------------------------------------------------------------------------

def test_extract_wasm_from_bundle(tmp_path: Path) -> None:
    p = _write_bundle(tmp_path / "b.tar.gz", wasm=b"fake-wasm")
    assert _extract_wasm_from_bundle(p) == b"fake-wasm"


def test_extract_data_json_from_bundle(tmp_path: Path) -> None:
    p = _write_bundle(tmp_path / "b.tar.gz", data={"required_features": ["sentiment"]})
    result = _extract_data_json_from_bundle(p)
    assert result == {"required_features": ["sentiment"]}


def test_extract_data_json_returns_none_when_absent(tmp_path: Path) -> None:
    p = _write_bundle(tmp_path / "b.tar.gz")
    assert _extract_data_json_from_bundle(p) is None


# ---------------------------------------------------------------------------
# RegoEvaluator — DISABLED mode
# ---------------------------------------------------------------------------

def test_evaluate_disabled_mode_returns_allow_without_engine() -> None:
    ev = RegoEvaluator(
        hook_wasm_paths={},
        enforcement_mode=EnforcementMode.DISABLED,
    )
    ctx = _make_context()
    record = ev.evaluate(ctx)
    assert record.final_action == Action.ALLOW
    assert record.metadata.get("rego_engine") == "skipped"


# ---------------------------------------------------------------------------
# RegoEvaluator — missing engine
# ---------------------------------------------------------------------------

def test_evaluate_missing_engine_returns_allow() -> None:
    ev, _ = _make_evaluator(hook=LifecycleHook.AFTER_AGENT)
    ctx = _make_context(hook=LifecycleHook.BEFORE_AGENT)
    record = ev.evaluate(ctx)
    assert record.final_action == Action.ALLOW
    assert record.metadata.get("rego_engine") == "missing"


# ---------------------------------------------------------------------------
# RegoEvaluator — ENFORCE mode, allow
# ---------------------------------------------------------------------------

def test_evaluate_enforce_allow_returns_audit_record() -> None:
    ev, _ = _make_evaluator(engine_result={"deny": False})
    ctx = _make_context()
    record = ev.evaluate(ctx)
    assert record.final_action == Action.ALLOW
    assert record.hook == LifecycleHook.BEFORE_MODEL


# ---------------------------------------------------------------------------
# RegoEvaluator — ENFORCE mode, deny raises GovernanceBlockException
# ---------------------------------------------------------------------------

def test_evaluate_enforce_deny_raises() -> None:
    ev, _ = _make_evaluator(engine_result={"deny": True, "fired_deny": ["pack/rule1"]})
    ctx = _make_context()
    with pytest.raises(GovernanceBlockException):
        ev.evaluate(ctx)


# ---------------------------------------------------------------------------
# RegoEvaluator — AUDIT mode, deny does NOT raise
# ---------------------------------------------------------------------------

def test_evaluate_audit_mode_deny_does_not_raise() -> None:
    ev, _ = _make_evaluator(
        engine_result={"deny": True, "fired_deny": ["pack/rule1"]},
        enforcement_mode=EnforcementMode.AUDIT,
    )
    ctx = _make_context()
    record = ev.evaluate(ctx)
    assert record.final_action == Action.AUDIT
    assert record.metadata.get("audit_mode_would_deny") is True


# ---------------------------------------------------------------------------
# RegoEvaluator — engine raises, fail-open
# ---------------------------------------------------------------------------

def test_evaluate_engine_exception_returns_allow() -> None:
    engine = MagicMock()
    engine.evaluate.side_effect = RuntimeError("wasm error")
    with patch("uipath.runtime.governance.rego.evaluator._load_engine", return_value=engine):
        ev = RegoEvaluator(
            hook_wasm_paths={LifecycleHook.BEFORE_MODEL: Path("/fake/bundle.tar.gz")},
            enforcement_mode=EnforcementMode.ENFORCE,
        )
    ctx = _make_context()
    record = ev.evaluate(ctx)
    assert record.final_action == Action.ALLOW
    assert record.metadata.get("rego_engine") == "error"


# ---------------------------------------------------------------------------
# RegoEvaluator — fired_deny / fired_allow rule evaluations
# ---------------------------------------------------------------------------

def test_evaluate_populates_rule_evaluations() -> None:
    ev, _ = _make_evaluator(engine_result={
        "deny": True,
        "fired_deny": ["pack/block_ssn"],
        "fired_allow": ["pack/safe_rule"],
        "messages": {"pack/block_ssn": "SSN detected"},
    })
    ctx = _make_context()
    with pytest.raises(GovernanceBlockException) as exc_info:
        ev.evaluate(ctx)
    # The audit record is embedded in the exception
    record = exc_info.value.audit_record
    deny_evals = [e for e in record.evaluations if e.action == Action.DENY]
    allow_evals = [e for e in record.evaluations if e.action == Action.ALLOW]
    assert len(deny_evals) == 1
    assert deny_evals[0].rule_id == "pack/block_ssn"
    assert deny_evals[0].detail == "SSN detected"
    assert len(allow_evals) == 1


# ---------------------------------------------------------------------------
# RegoEvaluator — opa_wasmtime not installed, load warning
# ---------------------------------------------------------------------------

def test_load_engine_import_error_skips_hook() -> None:
    with patch("uipath.runtime.governance.rego.evaluator._load_engine",
               side_effect=ImportError("opa_wasmtime not installed")):
        ev = RegoEvaluator(
            hook_wasm_paths={LifecycleHook.BEFORE_MODEL: Path("/fake/bundle.tar.gz")},
            enforcement_mode=EnforcementMode.ENFORCE,
        )
    assert LifecycleHook.BEFORE_MODEL not in ev._engines
    assert ev.loaded_hooks == []


# ---------------------------------------------------------------------------
# RegoEvaluator — audit emission
# ---------------------------------------------------------------------------

def test_evaluate_emits_audit_events() -> None:
    from uipath.runtime.governance._audit.base import AuditManager, AuditSink, AuditEvent

    class _Sink(AuditSink):
        def __init__(self) -> None:
            self.events: list[AuditEvent] = []
        @property
        def name(self) -> str:
            return "test-sink"
        def emit(self, event: AuditEvent) -> None:
            self.events.append(event)

    sink = _Sink()
    manager = AuditManager()
    manager.register_sink(sink)

    ev, _ = _make_evaluator(
        engine_result={"deny": False, "fired_allow": ["pack/safe"]},
        enforcement_mode=EnforcementMode.ENFORCE,
    )
    ev._audit_manager = manager

    ctx = _make_context()
    ev.evaluate(ctx)
    assert len(sink.events) > 0


# ---------------------------------------------------------------------------
# RegoEvaluator — convenience evaluate_* methods
# ---------------------------------------------------------------------------

def test_evaluate_before_model() -> None:
    ev, _ = _make_evaluator(engine_result={"deny": False})
    record = ev.evaluate_before_model("prompt", "agent", "run-1", model_name="gpt-4")
    assert record.hook == LifecycleHook.BEFORE_MODEL


def test_evaluate_before_agent() -> None:
    ev, _ = _make_evaluator(
        hook=LifecycleHook.BEFORE_AGENT,
        engine_result={"deny": False},
    )
    record = ev.evaluate_before_agent("input", "agent", "run-1")
    assert record.hook == LifecycleHook.BEFORE_AGENT


def test_evaluate_after_agent() -> None:
    ev, _ = _make_evaluator(
        hook=LifecycleHook.AFTER_AGENT,
        engine_result={"deny": False},
    )
    record = ev.evaluate_after_agent("output", "agent", "run-1")
    assert record.hook == LifecycleHook.AFTER_AGENT


def test_evaluate_tool_call() -> None:
    ev, _ = _make_evaluator(
        hook=LifecycleHook.TOOL_CALL,
        engine_result={"deny": False},
    )
    record = ev.evaluate_tool_call("my_tool", {"arg": "val"}, "agent", "run-1")
    assert record.hook == LifecycleHook.TOOL_CALL


def test_evaluate_after_tool() -> None:
    ev, _ = _make_evaluator(
        hook=LifecycleHook.AFTER_TOOL,
        engine_result={"deny": False},
    )
    record = ev.evaluate_after_tool("my_tool", "result", "agent", "run-1")
    assert record.hook == LifecycleHook.AFTER_TOOL


def test_evaluate_after_model() -> None:
    ev, _ = _make_evaluator(
        hook=LifecycleHook.AFTER_MODEL,
        engine_result={"deny": False},
    )
    record = ev.evaluate_after_model("output", "agent", "run-1")
    assert record.hook == LifecycleHook.AFTER_MODEL
