"""Tests for ConsoleAuditSink — human-readable governance logging."""
from __future__ import annotations

import logging
from typing import Any

import pytest

from uipath.runtime.governance._audit.base import AuditEvent, EventType
from uipath.runtime.governance._audit.console import ConsoleAuditSink


def _make_event(
    event_type: str,
    data: dict[str, Any] | None = None,
    hook: str = "before_model",
    agent_name: str = "test-agent",
) -> AuditEvent:
    return AuditEvent(
        event_type=event_type,
        agent_name=agent_name,
        hook=hook,
        data=data or {},
    )


# ---------------------------------------------------------------------------
# accepts()
# ---------------------------------------------------------------------------

def test_accepts_all_when_verbose() -> None:
    sink = ConsoleAuditSink(verbose=True)
    event = _make_event(EventType.RULE_EVALUATION, {"matched": False})
    assert sink.accepts(event) is True


def test_accepts_matched_rule_when_not_verbose() -> None:
    sink = ConsoleAuditSink(verbose=False)
    event = _make_event(EventType.RULE_EVALUATION, {"matched": True})
    assert sink.accepts(event) is True


def test_rejects_unmatched_rule_when_not_verbose() -> None:
    sink = ConsoleAuditSink(verbose=False)
    event = _make_event(EventType.RULE_EVALUATION, {"matched": False})
    assert sink.accepts(event) is False


def test_accepts_session_start_always() -> None:
    sink = ConsoleAuditSink(verbose=False)
    event = _make_event(EventType.SESSION_START)
    assert sink.accepts(event) is True


def test_accepts_session_end_always() -> None:
    sink = ConsoleAuditSink(verbose=False)
    event = _make_event(EventType.SESSION_END)
    assert sink.accepts(event) is True


def test_accepts_hook_end_always() -> None:
    sink = ConsoleAuditSink(verbose=False)
    event = _make_event(EventType.HOOK_END)
    assert sink.accepts(event) is True


def test_accepts_policy_violation_always() -> None:
    sink = ConsoleAuditSink(verbose=False)
    event = _make_event(EventType.POLICY_VIOLATION)
    assert sink.accepts(event) is True


# ---------------------------------------------------------------------------
# name property
# ---------------------------------------------------------------------------

def test_name_is_console() -> None:
    assert ConsoleAuditSink().name == "console"


# ---------------------------------------------------------------------------
# emit() — rule evaluation
# ---------------------------------------------------------------------------

def test_emit_matched_rule_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    sink = ConsoleAuditSink()
    event = _make_event(EventType.RULE_EVALUATION, {
        "matched": True,
        "policy_id": "p1",
        "rule_name": "block-ssn",
        "action": "deny",
        "detail": "SSN detected",
    })
    with caplog.at_level(logging.WARNING, logger="uipath.governance.audit.console"):
        sink.emit(event)
    assert "MATCHED" in caplog.text
    assert "block-ssn" in caplog.text


def test_emit_unmatched_rule_verbose_logs_info(caplog: pytest.LogCaptureFixture) -> None:
    sink = ConsoleAuditSink(verbose=True)
    event = _make_event(EventType.RULE_EVALUATION, {
        "matched": False,
        "policy_id": "p1",
        "rule_name": "allow-rule",
        "action": "allow",
    })
    with caplog.at_level(logging.INFO, logger="uipath.governance.audit.console"):
        sink.emit(event)
    assert "PASS" in caplog.text


def test_emit_unmatched_rule_non_verbose_no_log(caplog: pytest.LogCaptureFixture) -> None:
    sink = ConsoleAuditSink(verbose=False)
    event = _make_event(EventType.RULE_EVALUATION, {"matched": False})
    with caplog.at_level(logging.DEBUG, logger="uipath.governance.audit.console"):
        sink.emit(event)
    # No PASS or MATCHED should appear since verbose=False and matched=False
    assert "PASS" not in caplog.text
    assert "MATCHED" not in caplog.text


# ---------------------------------------------------------------------------
# emit() — hook summary
# ---------------------------------------------------------------------------

def test_emit_hook_summary_matched_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    from uipath.core.governance import EnforcementMode
    sink = ConsoleAuditSink()
    event = _make_event(EventType.HOOK_END, {
        "total_rules": 3,
        "matched_rules": 1,
        "final_action": "deny",
        "enforcement_mode": EnforcementMode.AUDIT,
    })
    with caplog.at_level(logging.WARNING, logger="uipath.governance.audit.console"):
        sink.emit(event)
    assert "AUDIT (would deny)" in caplog.text


def test_emit_hook_summary_no_match_logs_info(caplog: pytest.LogCaptureFixture) -> None:
    sink = ConsoleAuditSink()
    event = _make_event(EventType.HOOK_END, {
        "total_rules": 2,
        "matched_rules": 0,
        "final_action": "allow",
        "enforcement_mode": None,
    })
    with caplog.at_level(logging.INFO, logger="uipath.governance.audit.console"):
        sink.emit(event)
    assert "HOOK" in caplog.text


def test_emit_hook_summary_none_mode(caplog: pytest.LogCaptureFixture) -> None:
    sink = ConsoleAuditSink()
    event = _make_event(EventType.HOOK_END, {
        "total_rules": 1,
        "matched_rules": 0,
        "final_action": "allow",
        "enforcement_mode": None,
    })
    with caplog.at_level(logging.INFO, logger="uipath.governance.audit.console"):
        sink.emit(event)
    assert "HOOK" in caplog.text


# ---------------------------------------------------------------------------
# emit() — session start / end
# ---------------------------------------------------------------------------

def test_emit_session_start(caplog: pytest.LogCaptureFixture) -> None:
    from uipath.core.governance import EnforcementMode
    sink = ConsoleAuditSink()
    event = _make_event(EventType.SESSION_START, {
        "packs": ["pack-a", "pack-b"],
        "enforcement_mode": EnforcementMode.ENFORCE,
    }, agent_name="my-agent")
    with caplog.at_level(logging.INFO, logger="uipath.governance.audit.console"):
        sink.emit(event)
    assert "Session started" in caplog.text
    assert "my-agent" in caplog.text


def test_emit_session_start_no_mode(caplog: pytest.LogCaptureFixture) -> None:
    sink = ConsoleAuditSink()
    event = _make_event(EventType.SESSION_START, {
        "packs": [],
        "enforcement_mode": None,
    })
    with caplog.at_level(logging.INFO, logger="uipath.governance.audit.console"):
        sink.emit(event)
    assert "Session started" in caplog.text


def test_emit_session_end(caplog: pytest.LogCaptureFixture) -> None:
    sink = ConsoleAuditSink()
    event = _make_event(EventType.SESSION_END, {
        "total_evaluations": 10,
        "rules_matched": 2,
        "rules_denied": 1,
    })
    with caplog.at_level(logging.INFO, logger="uipath.governance.audit.console"):
        sink.emit(event)
    assert "Session ended" in caplog.text


# ---------------------------------------------------------------------------
# emit() — generic fallback
# ---------------------------------------------------------------------------

def test_emit_generic_event(caplog: pytest.LogCaptureFixture) -> None:
    sink = ConsoleAuditSink()
    event = _make_event("custom_event_type", {"foo": "bar"})
    with caplog.at_level(logging.INFO, logger="uipath.governance.audit.console"):
        sink.emit(event)
    assert "custom_event_type" in caplog.text
