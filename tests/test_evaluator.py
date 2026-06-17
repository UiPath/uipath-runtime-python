"""Tests for the audit + enforcement behavior of GovernanceEvaluator.

The evaluator owns three responsibilities that used to be scattered
across wrapper.py and adapter callbacks:

1. DISABLED enforcement mode short-circuits — no rules evaluated, no
   audit events emitted, no exceptions raised.
2. AUDIT mode evaluates rules and emits audit events, but transforms
   matched DENY actions into AUDIT so execution continues.
3. ENFORCE mode evaluates, emits audit, and raises
   :class:`GovernanceBlockException` when a DENY rule matches.

Plus a fail-safe contract: a misbehaving audit sink must not stop
evaluation from completing or propagate as an exception.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from uipath.core.governance.exceptions import GovernanceBlockException
from uipath.core.governance.models import Action, LifecycleHook

from tests._helpers import reset_enforcement_mode
from uipath.runtime.governance.audit import (
    AuditEvent,
    AuditSink,
    EventType,
    get_audit_manager,
    reset_audit_manager,
)
from uipath.runtime.governance.config import (
    EnforcementMode,
    set_enforcement_mode,
)
from uipath.runtime.governance.native.evaluator import GovernanceEvaluator
from uipath.runtime.governance.native.models import (
    Check,
    CheckContext,
    Condition,
    PolicyIndex,
    PolicyPack,
    Rule,
)

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class _CapturingSink(AuditSink):
    """Audit sink that records every event for assertions."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    @property
    def name(self) -> str:
        return "capturing"

    def emit(self, event: AuditEvent) -> None:
        self.events.append(event)


def _deny_rule_on_input_contains(needle: str) -> Rule:
    """Build a rule that DENIES when agent_input contains ``needle``."""
    return Rule(
        rule_id="TEST-01",
        name="Test deny on input",
        clause="A.1.1",
        hook=LifecycleHook.BEFORE_AGENT,
        action=Action.DENY,
        checks=[
            Check(
                conditions=[
                    Condition(
                        operator="contains",
                        field="agent_input",
                        value=needle,
                    )
                ],
                action=Action.DENY,
                message=f"Input must not contain {needle!r}",
            )
        ],
    )


def _build_index_with(rule: Rule) -> PolicyIndex:
    """Wrap a single rule in a one-pack PolicyIndex."""
    idx = PolicyIndex()
    idx.add_pack(
        PolicyPack(
            name="test_pack",
            version="1.0",
            description="test",
            rules=[rule],
        )
    )
    return idx


def _ctx(agent_input: str) -> CheckContext:
    return CheckContext(
        hook=LifecycleHook.BEFORE_AGENT,
        agent_name="test-agent",
        runtime_id="run-1",
        trace_id="trace-1",
        agent_input=agent_input,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def capturing_audit():
    """Replace the global audit manager with a fresh one wired to a capturing sink.

    Yields the sink so tests can inspect emitted events. Restores the
    global manager on teardown.
    """
    reset_audit_manager()
    manager = get_audit_manager()
    # Default sinks (traces / console) are noisy here — drop them.
    for existing_name in list(manager.list_sinks()):
        manager.unregister_sink(existing_name)
    sink = _CapturingSink()
    manager.register_sink(sink)
    # Force synchronous emission so assertions don't race the worker thread.
    manager._async_mode = False
    yield sink
    reset_audit_manager()


@pytest.fixture(autouse=True)
def _reset_enforcement_mode():
    """Each test gets a clean enforcement-mode slate."""
    reset_enforcement_mode()
    yield
    reset_enforcement_mode()


# ---------------------------------------------------------------------------
# DISABLED mode
# ---------------------------------------------------------------------------


def test_disabled_mode_short_circuits_with_empty_record(capturing_audit):
    """DISABLED returns an empty AuditRecord and emits nothing."""
    set_enforcement_mode(EnforcementMode.DISABLED)
    evaluator = GovernanceEvaluator(
        _build_index_with(_deny_rule_on_input_contains("secret"))
    )

    audit = evaluator.evaluate(_ctx("definitely contains secret"))

    assert audit.evaluations == []
    assert audit.final_action == Action.ALLOW
    assert audit.metadata["enforcement_mode"] == "disabled"
    assert capturing_audit.events == []


def test_disabled_mode_does_not_raise_on_deny_match(capturing_audit):
    """Even when a DENY rule WOULD match, DISABLED never raises."""
    set_enforcement_mode(EnforcementMode.DISABLED)
    evaluator = GovernanceEvaluator(
        _build_index_with(_deny_rule_on_input_contains("blocked"))
    )

    # Must not raise.
    evaluator.evaluate(_ctx("this is blocked"))


# ---------------------------------------------------------------------------
# AUDIT mode
# ---------------------------------------------------------------------------


def test_audit_mode_transforms_deny_to_audit(capturing_audit):
    """AUDIT mode evaluates rules but never returns a DENY final_action."""
    set_enforcement_mode(EnforcementMode.AUDIT)
    evaluator = GovernanceEvaluator(
        _build_index_with(_deny_rule_on_input_contains("secret"))
    )

    audit = evaluator.evaluate(_ctx("contains secret data"))

    assert len(audit.evaluations) == 1
    assert audit.evaluations[0].matched is True
    assert audit.evaluations[0].action == Action.DENY  # raw rule action preserved
    assert audit.final_action == Action.AUDIT  # mode-adjusted
    assert audit.metadata["audit_mode_would_deny"] is True


def test_audit_mode_does_not_raise_on_deny_match(capturing_audit):
    """AUDIT mode never raises GovernanceBlockException, even on a DENY hit."""
    set_enforcement_mode(EnforcementMode.AUDIT)
    evaluator = GovernanceEvaluator(
        _build_index_with(_deny_rule_on_input_contains("blocked"))
    )

    evaluator.evaluate(_ctx("this is blocked"))  # must not raise


def test_audit_mode_emits_per_rule_and_summary_events(capturing_audit):
    """One rule_evaluation event per rule + one hook_summary per evaluate()."""
    set_enforcement_mode(EnforcementMode.AUDIT)
    evaluator = GovernanceEvaluator(
        _build_index_with(_deny_rule_on_input_contains("secret"))
    )

    evaluator.evaluate(_ctx("contains secret"))

    rule_events = [
        e for e in capturing_audit.events if e.event_type == EventType.RULE_EVALUATION
    ]
    summary_events = [
        e for e in capturing_audit.events if e.event_type == EventType.HOOK_END
    ]
    assert len(rule_events) == 1
    assert rule_events[0].hook == "BEFORE_AGENT"
    assert rule_events[0].data["rule_id"] == "TEST-01"
    assert rule_events[0].data["matched"] is True
    assert rule_events[0].data["action"] == "deny"

    assert len(summary_events) == 1
    assert summary_events[0].data["matched_rules"] == 1
    assert summary_events[0].data["final_action"] == "audit"
    assert summary_events[0].data["enforcement_mode"] == "audit"


def test_audit_mode_unmatched_rule_logged_as_allow(capturing_audit):
    """Unmatched rules still emit a rule_evaluation event with action='allow'."""
    set_enforcement_mode(EnforcementMode.AUDIT)
    evaluator = GovernanceEvaluator(
        _build_index_with(_deny_rule_on_input_contains("secret"))
    )

    evaluator.evaluate(_ctx("benign user query"))

    rule_events = [
        e for e in capturing_audit.events if e.event_type == EventType.RULE_EVALUATION
    ]
    assert len(rule_events) == 1
    assert rule_events[0].data["matched"] is False
    assert rule_events[0].data["action"] == "allow"


# ---------------------------------------------------------------------------
# ENFORCE mode
# ---------------------------------------------------------------------------


def test_enforce_mode_raises_on_deny_match(capturing_audit):
    """ENFORCE mode raises GovernanceBlockException when a DENY rule matches."""
    set_enforcement_mode(EnforcementMode.ENFORCE)
    evaluator = GovernanceEvaluator(
        _build_index_with(_deny_rule_on_input_contains("blocked"))
    )

    with pytest.raises(GovernanceBlockException) as exc_info:
        evaluator.evaluate(_ctx("input is blocked"))

    exc = exc_info.value
    assert exc.rule_id == "TEST-01"
    assert exc.rule_name == "Test deny on input"
    assert exc.audit_record is not None
    assert exc.audit_record.final_action == Action.DENY


def test_enforce_mode_emits_audit_before_raising(capturing_audit):
    """The audit trail must be emitted even when the call raises."""
    set_enforcement_mode(EnforcementMode.ENFORCE)
    evaluator = GovernanceEvaluator(
        _build_index_with(_deny_rule_on_input_contains("blocked"))
    )

    with pytest.raises(GovernanceBlockException):
        evaluator.evaluate(_ctx("contains blocked"))

    rule_events = [
        e for e in capturing_audit.events if e.event_type == EventType.RULE_EVALUATION
    ]
    summary_events = [
        e for e in capturing_audit.events if e.event_type == EventType.HOOK_END
    ]
    assert len(rule_events) == 1
    assert summary_events[0].data["final_action"] == "deny"
    assert summary_events[0].data["enforcement_mode"] == "enforce"


def test_enforce_mode_returns_record_when_no_rule_matches(capturing_audit):
    """No DENY hit → no raise; the AuditRecord is returned normally."""
    set_enforcement_mode(EnforcementMode.ENFORCE)
    evaluator = GovernanceEvaluator(
        _build_index_with(_deny_rule_on_input_contains("blocked"))
    )

    audit = evaluator.evaluate(_ctx("benign query"))

    assert audit.final_action == Action.ALLOW
    assert audit.evaluations[0].matched is False


# ---------------------------------------------------------------------------
# Sink-failure isolation
# ---------------------------------------------------------------------------


def test_sink_failure_does_not_propagate_or_block_evaluation(capturing_audit):
    """A broken sink must not make evaluate() raise or lose its return value.

    The contract: AuditManager wraps each sink's emit() in try/except with
    a per-sink failure counter (circuit-breaker), so an exception inside a
    sink never propagates back to the evaluator.
    """

    class _BrokenSink(AuditSink):
        @property
        def name(self) -> str:
            return "broken"

        def emit(self, event: AuditEvent) -> None:
            raise RuntimeError("sink broke")

    manager = get_audit_manager()
    manager.register_sink(_BrokenSink())

    set_enforcement_mode(EnforcementMode.AUDIT)
    evaluator = GovernanceEvaluator(
        _build_index_with(_deny_rule_on_input_contains("secret"))
    )

    # Must complete without raising even with a broken sink registered.
    audit = evaluator.evaluate(_ctx("contains secret"))

    assert audit.final_action == Action.AUDIT
    # The non-broken capturing sink still got its events.
    assert any(
        e.event_type == EventType.RULE_EVALUATION for e in capturing_audit.events
    )


def test_unavailable_audit_manager_is_swallowed():
    """If get_audit_manager() itself raises, _emit_audit must swallow it."""
    set_enforcement_mode(EnforcementMode.AUDIT)
    evaluator = GovernanceEvaluator(
        _build_index_with(_deny_rule_on_input_contains("secret"))
    )

    with patch(
        "uipath.runtime.governance.native.evaluator.get_audit_manager",
        side_effect=RuntimeError("manager unavailable"),
    ):
        # Must complete, return record, and not raise.
        audit = evaluator.evaluate(_ctx("contains secret"))

    assert audit.final_action == Action.AUDIT
    assert audit.evaluations[0].matched is True


# ---------------------------------------------------------------------------
# Protocol conformance smoke test
# ---------------------------------------------------------------------------


def test_governance_evaluator_satisfies_evaluator_protocol():
    """GovernanceEvaluator must be usable wherever EvaluatorProtocol is expected.

    Mirrors the pattern from test_detached_bridge_satisfies_debug_protocol —
    an explicit assignment to the protocol-typed variable documents the
    structural contract.
    """
    from uipath.core.adapters import EvaluatorProtocol

    evaluator: EvaluatorProtocol = GovernanceEvaluator(PolicyIndex())
    assert isinstance(evaluator, EvaluatorProtocol)


def test_evaluator_protocol_methods_resolvable_on_concrete():
    """Every method the protocol declares must be callable on the concrete impl."""
    from uipath.core.adapters import EvaluatorProtocol

    evaluator: Any = GovernanceEvaluator(PolicyIndex())
    for method_name in (
        "evaluate_before_agent",
        "evaluate_after_agent",
        "evaluate_before_model",
        "evaluate_after_model",
        "evaluate_tool_call",
        "evaluate_after_tool",
    ):
        assert callable(getattr(evaluator, method_name))
    # The variable annotation also asserts type compatibility at runtime
    # because EvaluatorProtocol is @runtime_checkable.
    assert isinstance(evaluator, EvaluatorProtocol)
