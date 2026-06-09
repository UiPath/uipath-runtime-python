"""Tests for trace-span severity / status semantics.

``TracesAuditSink`` emits an OpenTelemetry span for every governance
hook end and every rule evaluation. The span's ``Status`` drives whether
backends (Orchestrator Traces UI, generic OTel viewers) render the
trace as an error.

Contract:
- A matched rule with a non-``allow`` action sets the **rule span** to
  ``StatusCode.ERROR`` — including ``audit`` and ``escalate`` actions,
  not just ``deny``. Audit-mode violations (which the runtime doesn't
  block) still surface as errors at the rule level.
- **Hook spans are never marked ERROR**, regardless of ``final_action``.
  They're summary containers; severity belongs on the individual rule
  span that actually fired. Marking a hook span as ERROR would falsely
  paint the entire ``before_model`` / ``after_model`` phase as failed
  when only one rule underneath it violated.
- ``allow`` actions (or unmatched rules) leave ``Status`` at the default
  ``UNSET``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from uipath.runtime.governance.audit.base import AuditEvent, EventType
from uipath.runtime.governance.audit.traces import TracesAuditSink


@pytest.fixture
def captured_span(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Wire ``TracesAuditSink`` to a mock tracer and return the span mock.

    The sink lazily resolves the tracer via ``_get_tracer``. We patch that
    to return a tracer whose ``start_as_current_span`` context manager
    yields a MagicMock — the test then asserts against ``set_status`` /
    ``set_attribute`` on that mock.
    """
    span = MagicMock(name="span")
    tracer = MagicMock(name="tracer")
    tracer.start_as_current_span.return_value.__enter__.return_value = span
    tracer.start_as_current_span.return_value.__exit__.return_value = False
    monkeypatch.setattr(TracesAuditSink, "_get_tracer", lambda self: tracer)
    return span


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


# ----------------------------------------------------------------------------
# Hook span — never marked ERROR; severity belongs on the rule span
# ----------------------------------------------------------------------------


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
    """Hook spans are summary containers — they never carry an ERROR Status.

    A hook span aggregates many rule evaluations; marking it ERROR would
    paint the whole lifecycle phase (e.g. ``before_model``) as failed
    when only one rule underneath fired. Severity lives on the per-rule
    spans.
    """
    sink = TracesAuditSink()
    sink.emit(_hook_event(final_action=final_action, mode=mode))
    assert not captured_span.set_status.called, (
        f"Hook span should never set_status; called with "
        f"final_action={final_action!r}, mode={mode!r}"
    )


# ----------------------------------------------------------------------------
# Rule span — matched + non-allow action drives Status
# ----------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["deny", "audit", "escalate"])
def test_rule_span_sets_error_when_matched_non_allow(
    captured_span: MagicMock, action: str
) -> None:
    """Matched rule with any non-allow action surfaces as ERROR."""
    sink = TracesAuditSink()
    sink.emit(_rule_event(matched=True, action=action))

    assert captured_span.set_status.called, (
        f"set_status should fire for matched rule with action={action!r}"
    )
    status_code, message = captured_span.set_status.call_args.args
    from opentelemetry.trace import StatusCode

    assert status_code is StatusCode.ERROR
    assert "commitment-language" in message
    assert action in message


def test_rule_span_does_not_set_error_when_not_matched(
    captured_span: MagicMock,
) -> None:
    """An unmatched evaluation never marks the span as error."""
    sink = TracesAuditSink()
    sink.emit(_rule_event(matched=False, action="deny"))
    assert not captured_span.set_status.called


def test_rule_span_does_not_set_error_when_matched_action_allow(
    captured_span: MagicMock,
) -> None:
    """A rule whose action is 'allow' is explicit non-violation; no ERROR."""
    sink = TracesAuditSink()
    sink.emit(_rule_event(matched=True, action="allow"))
    assert not captured_span.set_status.called
