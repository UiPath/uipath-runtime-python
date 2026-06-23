"""Tests for trace-span verbosity / status semantics.

``TracesAuditSink`` emits an OpenTelemetry span for every governance
hook end and every rule evaluation. The contract follows §4 of the
cross-product unification doc — verdict is split into ``evaluator_result``
(what the rule decided, mode-independent) and ``action_applied`` (what
actually happened, derived from evaluator_result + mode).

- ``verbosityLevel = 4`` (Error) and ``StatusCode.ERROR`` fire **only**
  when ``action_applied = DENY`` — i.e. the runtime actually blocked
  the agent (ENFORCE mode + configured action ``deny``).
- ``verbosityLevel = 3`` (Warning) and ``Status.UNSET`` for advisory
  outcomes (``action_applied`` in ``{AUDIT, HITL}``). HITL is its own
  spec bucket — escalation pauses for human review, it doesn't fail
  the run, so it stays Warning even in ENFORCE mode.
- Hook spans never set Status, regardless of mode or final_action.
  They're summary containers; severity belongs on the per-rule span.
- ``ALLOW`` / ``NONE`` results leave verbosityLevel unset (platform
  default = 2, Information) and never call set_status.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tests._helpers import reset_enforcement_mode
from uipath.runtime.governance._audit.base import AuditEvent, EventType
from uipath.runtime.governance._audit.traces import TracesAuditSink
from uipath.runtime.governance.config import (
    EnforcementMode,
    set_enforcement_mode,
)


@pytest.fixture
def captured_span(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Wire ``TracesAuditSink`` to a mock tracer and return the span mock."""
    span = MagicMock(name="span")
    tracer = MagicMock(name="tracer")
    tracer.start_as_current_span.return_value.__enter__.return_value = span
    tracer.start_as_current_span.return_value.__exit__.return_value = False
    monkeypatch.setattr(TracesAuditSink, "_get_tracer", lambda self: tracer)
    return span


@pytest.fixture(autouse=True)
def _reset_mode() -> None:
    """Each test selects its own enforcement mode explicitly."""
    reset_enforcement_mode()
    yield
    reset_enforcement_mode()


def _hook_event(final_action: str, mode: str = "audit") -> AuditEvent:
    return AuditEvent(
        event_type=EventType.HOOK_END,
        agent_name="agent",
        hook="after_model",
        data={
            "total_rules": 1,
            "matched_rules": 1 if final_action != "allow" else 0,
            "final_action": final_action,
            "enforcement_mode": mode,
        },
    )


def _rule_event(matched: bool, action: str) -> AuditEvent:
    return AuditEvent(
        event_type=EventType.RULE_EVALUATION,
        agent_name="agent",
        hook="after_model",
        data={
            "policy_id": "A.10.4",
            "rule_name": "commitment-language",
            "pack_name": "iso42001",
            "matched": matched,
            "action": action,
            "status": "MATCHED" if matched else "PASS",
            "detail": "Customer-binding commitment detected.",
        },
    )


def _span_attrs(span: MagicMock) -> dict[str, object]:
    """Return a mapping of attribute name → value for set_attribute calls."""
    attrs: dict[str, object] = {}
    for call in span.set_attribute.call_args_list:
        key, value = call.args
        attrs[key] = value
    return attrs


# ---------------------------------------------------------------------------
# Hook span — never marked ERROR
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "final_action,mode",
    [
        ("deny", "enforce"),
        ("deny", "audit"),
        ("audit", "audit"),
        ("escalate", "audit"),
        ("allow", "audit"),
    ],
)
def test_hook_span_never_sets_error(
    captured_span: MagicMock, final_action: str, mode: str
) -> None:
    """Hook spans are summary containers — they never carry an ERROR Status."""
    sink = TracesAuditSink()
    sink.emit(_hook_event(final_action=final_action, mode=mode))
    assert not captured_span.set_status.called, (
        f"Hook span should never set_status; called with "
        f"final_action={final_action!r}, mode={mode!r}"
    )


# ---------------------------------------------------------------------------
# Rule span — enforce-mode DENY is the only Status.ERROR case
# ---------------------------------------------------------------------------


def test_enforce_mode_deny_is_error(captured_span: MagicMock) -> None:
    """Enforce mode + action=deny = real block → verbosityLevel=4 + Status.ERROR."""
    set_enforcement_mode(EnforcementMode.ENFORCE)
    sink = TracesAuditSink()
    sink.emit(_rule_event(matched=True, action="deny"))

    attrs = _span_attrs(captured_span)
    assert attrs.get("verbosityLevel") == 4
    assert attrs.get("uipath_governance.evaluator_result") == "DENY"
    assert attrs.get("uipath_governance.action_applied") == "DENY"
    assert attrs.get("uipath_governance.mode") == "ENFORCE"

    assert captured_span.set_status.called, (
        "Status.ERROR must fire for enforce-mode deny violation"
    )
    (status_arg,) = captured_span.set_status.call_args.args
    from opentelemetry.trace import Status, StatusCode

    assert isinstance(status_arg, Status)
    assert status_arg.status_code is StatusCode.ERROR
    assert "commitment-language" in status_arg.description
    assert "deny" in status_arg.description


def test_enforce_mode_escalate_is_hitl_warning(captured_span: MagicMock) -> None:
    """Enforce mode + action=escalate = HITL pause, not a block.

    HITL is its own spec bucket distinct from DENY — escalation pauses
    for human review, the run isn't failed. So verbosityLevel stays at
    Warning and Status is not marked ERROR.
    """
    set_enforcement_mode(EnforcementMode.ENFORCE)
    sink = TracesAuditSink()
    sink.emit(_rule_event(matched=True, action="escalate"))

    attrs = _span_attrs(captured_span)
    assert attrs.get("verbosityLevel") == 3
    assert attrs.get("uipath_governance.evaluator_result") == "HITL"
    assert attrs.get("uipath_governance.action_applied") == "HITL"
    assert attrs.get("uipath_governance.mode") == "ENFORCE"
    assert not captured_span.set_status.called


# ---------------------------------------------------------------------------
# Rule span — advisory violations (audit mode, or audit-action rules)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action,expected_evaluator",
    [("deny", "DENY"), ("audit", "DENY"), ("escalate", "HITL")],
)
def test_audit_mode_violation_is_warning(
    captured_span: MagicMock, action: str, expected_evaluator: str
) -> None:
    """Audit mode never blocks → action_applied=AUDIT, verbosityLevel=3.

    Surfacing Status.ERROR for an audit-mode violation would falsely
    mark the agent's run as failed when the runtime intentionally
    let it through. evaluator_result still records the rule's actual
    decision (DENY/HITL), independent of mode.
    """
    set_enforcement_mode(EnforcementMode.AUDIT)
    sink = TracesAuditSink()
    sink.emit(_rule_event(matched=True, action=action))

    attrs = _span_attrs(captured_span)
    assert attrs.get("verbosityLevel") == 3
    assert attrs.get("uipath_governance.evaluator_result") == expected_evaluator
    assert attrs.get("uipath_governance.action_applied") == "AUDIT"
    assert attrs.get("uipath_governance.mode") == "AUDIT"

    assert not captured_span.set_status.called, (
        f"Audit-mode {action} violation must NOT set Status.ERROR"
    )


def test_enforce_mode_audit_action_is_warning(captured_span: MagicMock) -> None:
    """Enforce mode + action=audit is a per-rule audit override.

    The rule's configured ``audit`` action means "log this match but
    don't block" even when the global mode is ENFORCE. evaluator_result
    is DENY (the rule decided to deny), but action_applied is AUDIT
    (the per-rule override kicks in), so verbosity stays Warning.
    """
    set_enforcement_mode(EnforcementMode.ENFORCE)
    sink = TracesAuditSink()
    sink.emit(_rule_event(matched=True, action="audit"))

    attrs = _span_attrs(captured_span)
    assert attrs.get("verbosityLevel") == 3
    assert attrs.get("uipath_governance.evaluator_result") == "DENY"
    assert attrs.get("uipath_governance.action_applied") == "AUDIT"
    assert attrs.get("uipath_governance.mode") == "ENFORCE"
    assert not captured_span.set_status.called


# ---------------------------------------------------------------------------
# Rule span — no violation, no verbosityLevel attribute (platform default = 2)
# ---------------------------------------------------------------------------


def test_unmatched_rule_no_verbosity_no_error(captured_span: MagicMock) -> None:
    """Unmatched evaluations → evaluator_result=ALLOW, action_applied=NONE, quiet."""
    set_enforcement_mode(EnforcementMode.ENFORCE)
    sink = TracesAuditSink()
    sink.emit(_rule_event(matched=False, action="deny"))

    attrs = _span_attrs(captured_span)
    assert "verbosityLevel" not in attrs
    assert attrs.get("uipath_governance.evaluator_result") == "ALLOW"
    assert attrs.get("uipath_governance.action_applied") == "NONE"
    assert not captured_span.set_status.called


def test_matched_allow_action_no_verbosity(captured_span: MagicMock) -> None:
    """A rule whose action is 'allow' is an explicit non-violation."""
    set_enforcement_mode(EnforcementMode.ENFORCE)
    sink = TracesAuditSink()
    sink.emit(_rule_event(matched=True, action="allow"))

    attrs = _span_attrs(captured_span)
    assert "verbosityLevel" not in attrs
    assert attrs.get("uipath_governance.evaluator_result") == "ALLOW"
    assert attrs.get("uipath_governance.action_applied") == "NONE"
    assert not captured_span.set_status.called
