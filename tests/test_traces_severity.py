"""Tests for trace-span verbosity / status semantics.

``TracesAuditSink`` emits an OpenTelemetry span for every governance
hook end and every rule evaluation. The contract:

- Matched non-allow rules carry a ``verbosityLevel`` span attribute
  (UiPath Orchestrator log levels: 3=Warning, 4=Error). Platform default
  is 2 (Information); we only emit this attribute when a violation
  warrants Warning or Error. OTel ``StatusCode`` only has OK / ERROR /
  UNSET, so verbosityLevel is the channel that distinguishes
  "audit-mode advisory violation" from "actually blocked the agent".
- ``verbosityLevel = 4`` (Error) and ``StatusCode.ERROR`` fire **only**
  when the runtime actually blocked the agent — enforce mode AND the
  rule's action is ``deny`` or ``escalate``.
- ``verbosityLevel = 3`` (Warning) and ``Status.UNSET`` for advisory
  violations — audit mode (any non-allow action), or audit-action rules
  even in enforce mode. The agent didn't fail; surfacing Status.ERROR
  would falsely paint a successful run as a failure.
- Hook spans never set Status, regardless of enforcement mode or
  final_action. They're summary containers; verbosityLevel belongs on
  the individual rule span that fired.
- ``allow`` actions and unmatched evaluations leave Status at UNSET and
  do not emit a verbosityLevel attribute (platform default applies).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tests._helpers import reset_enforcement_mode
from uipath.runtime.governance.audit.base import AuditEvent, EventType
from uipath.runtime.governance.audit.traces import TracesAuditSink
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
            "rule_id": "A.10.4",
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
# Rule span — enforce-mode actually-blocking violations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["deny", "escalate"])
def test_enforce_mode_blocking_violation_is_error(
    captured_span: MagicMock, action: str
) -> None:
    """Enforce mode + deny/escalate = real failure → verbosityLevel=4 + Status.ERROR."""
    set_enforcement_mode(EnforcementMode.ENFORCE)
    sink = TracesAuditSink()
    sink.emit(_rule_event(matched=True, action=action))

    attrs = _span_attrs(captured_span)
    assert attrs.get("verbosityLevel") == 4
    assert "severity" not in attrs
    assert "governance.severity" not in attrs

    assert captured_span.set_status.called, (
        f"Status.ERROR must fire for enforce-mode {action} violation"
    )
    (status_arg,) = captured_span.set_status.call_args.args
    from opentelemetry.trace import Status, StatusCode

    assert isinstance(status_arg, Status)
    assert status_arg.status_code is StatusCode.ERROR
    assert "commitment-language" in status_arg.description
    assert action in status_arg.description


# ---------------------------------------------------------------------------
# Rule span — advisory violations (audit mode, or audit-action rules)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["deny", "audit", "escalate"])
def test_audit_mode_violation_is_warning(
    captured_span: MagicMock, action: str
) -> None:
    """Audit mode never blocks → verbosityLevel=3, Status.UNSET.

    Surfacing Status.ERROR for an audit-mode violation would falsely
    mark the agent's run as failed when the runtime intentionally
    let it through.
    """
    set_enforcement_mode(EnforcementMode.AUDIT)
    sink = TracesAuditSink()
    sink.emit(_rule_event(matched=True, action=action))

    attrs = _span_attrs(captured_span)
    assert attrs.get("verbosityLevel") == 3
    assert "severity" not in attrs
    assert "governance.severity" not in attrs

    assert not captured_span.set_status.called, (
        f"Audit-mode {action} violation must NOT set Status.ERROR"
    )


def test_enforce_mode_audit_action_is_warning(captured_span: MagicMock) -> None:
    """Enforce mode + action=audit is still advisory → verbosityLevel=3.

    An ``audit`` action means "log this match but don't block" even
    when the policy is in enforce mode. The runtime doesn't block;
    verbosity stays Warning.
    """
    set_enforcement_mode(EnforcementMode.ENFORCE)
    sink = TracesAuditSink()
    sink.emit(_rule_event(matched=True, action="audit"))

    attrs = _span_attrs(captured_span)
    assert attrs.get("verbosityLevel") == 3
    assert not captured_span.set_status.called


# ---------------------------------------------------------------------------
# Rule span — no violation, no verbosityLevel attribute (platform default = 2)
# ---------------------------------------------------------------------------


def test_unmatched_rule_no_verbosity_no_error(captured_span: MagicMock) -> None:
    """Unmatched evaluations are quiet: no verbosityLevel attr, no Status."""
    set_enforcement_mode(EnforcementMode.ENFORCE)
    sink = TracesAuditSink()
    sink.emit(_rule_event(matched=False, action="deny"))

    attrs = _span_attrs(captured_span)
    assert "verbosityLevel" not in attrs
    assert not captured_span.set_status.called


def test_matched_allow_action_no_verbosity(captured_span: MagicMock) -> None:
    """A rule whose action is 'allow' is an explicit non-violation."""
    set_enforcement_mode(EnforcementMode.ENFORCE)
    sink = TracesAuditSink()
    sink.emit(_rule_event(matched=True, action="allow"))

    attrs = _span_attrs(captured_span)
    assert "verbosityLevel" not in attrs
    assert not captured_span.set_status.called
