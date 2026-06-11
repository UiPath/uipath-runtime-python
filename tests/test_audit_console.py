"""Tests for ``ConsoleAuditSink``.

The console sink is a developer-aid that writes governance events to
stderr in a human-readable format. Filtering and per-event-type
formatting are the things worth pinning so a non-verbose run doesn't
spam unmatched evaluations.
"""

from __future__ import annotations

import pytest

from uipath.runtime.governance.audit.base import AuditEvent, EventType
from uipath.runtime.governance.audit.console import ConsoleAuditSink

# ---------------------------------------------------------------------------
# Basic surface
# ---------------------------------------------------------------------------


def test_sink_name_is_console() -> None:
    assert ConsoleAuditSink().name == "console"


def test_default_is_non_verbose() -> None:
    """Constructor default keeps the sink quiet (matches-only)."""
    sink = ConsoleAuditSink()
    unmatched = AuditEvent(
        event_type=EventType.RULE_EVALUATION,
        data={"matched": False, "rule_id": "A", "rule_name": "n"},
    )
    assert sink.accepts(unmatched) is False


# ---------------------------------------------------------------------------
# accepts() — filtering behavior
# ---------------------------------------------------------------------------


def test_accepts_verbose_passes_everything() -> None:
    sink = ConsoleAuditSink(verbose=True)
    assert sink.accepts(AuditEvent(event_type=EventType.RULE_EVALUATION)) is True
    assert sink.accepts(AuditEvent(event_type=EventType.HOOK_END)) is True
    assert sink.accepts(AuditEvent(event_type=EventType.PACKS_LOADED)) is True


def test_accepts_non_verbose_filters_unmatched_rule_eval() -> None:
    sink = ConsoleAuditSink(verbose=False)
    matched = AuditEvent(
        event_type=EventType.RULE_EVALUATION, data={"matched": True}
    )
    unmatched = AuditEvent(
        event_type=EventType.RULE_EVALUATION, data={"matched": False}
    )
    assert sink.accepts(matched) is True
    assert sink.accepts(unmatched) is False


@pytest.mark.parametrize(
    "event_type",
    [
        EventType.SESSION_START,
        EventType.SESSION_END,
        EventType.HOOK_END,
        EventType.POLICY_VIOLATION,
    ],
)
def test_accepts_non_verbose_passes_lifecycle_events(event_type: str) -> None:
    """Lifecycle events flow through even when verbose is off."""
    sink = ConsoleAuditSink(verbose=False)
    assert sink.accepts(AuditEvent(event_type=event_type)) is True


def test_accepts_non_verbose_drops_other_event_types() -> None:
    sink = ConsoleAuditSink(verbose=False)
    # PACKS_LOADED isn't in the lifecycle allowlist for non-verbose.
    assert sink.accepts(AuditEvent(event_type=EventType.PACKS_LOADED)) is False


# ---------------------------------------------------------------------------
# _emit_rule_evaluation
# ---------------------------------------------------------------------------


def test_emit_matched_rule_writes_full_line(capsys: pytest.CaptureFixture[str]) -> None:
    sink = ConsoleAuditSink(verbose=False)
    sink.emit(
        AuditEvent(
            event_type=EventType.RULE_EVALUATION,
            data={
                "matched": True,
                "rule_id": "A.10.4",
                "rule_name": "commitment-language",
                "action": "audit",
                "detail": "Customer commitment detected.",
            },
        )
    )
    out = capsys.readouterr().err
    assert "MATCHED" in out
    assert "A.10.4" in out
    assert "commitment-language" in out
    assert "action=AUDIT" in out
    assert "Customer commitment detected." in out


def test_emit_unmatched_rule_silent_when_non_verbose(
    capsys: pytest.CaptureFixture[str],
) -> None:
    sink = ConsoleAuditSink(verbose=False)
    sink.emit(
        AuditEvent(
            event_type=EventType.RULE_EVALUATION,
            data={"matched": False, "rule_id": "A", "rule_name": "n"},
        )
    )
    assert capsys.readouterr().err == ""


def test_emit_unmatched_rule_prints_pass_when_verbose(
    capsys: pytest.CaptureFixture[str],
) -> None:
    sink = ConsoleAuditSink(verbose=True)
    sink.emit(
        AuditEvent(
            event_type=EventType.RULE_EVALUATION,
            data={"matched": False, "rule_id": "A.1", "rule_name": "rule-one"},
        )
    )
    out = capsys.readouterr().err
    assert "PASS" in out
    assert "A.1" in out
    assert "rule-one" in out


# ---------------------------------------------------------------------------
# _emit_hook_summary
# ---------------------------------------------------------------------------


def test_emit_hook_summary_basic(capsys: pytest.CaptureFixture[str]) -> None:
    sink = ConsoleAuditSink(verbose=False)
    sink.emit(
        AuditEvent(
            event_type=EventType.HOOK_END,
            hook="after_model",
            data={
                "total_rules": 5,
                "matched_rules": 1,
                "final_action": "allow",
                "enforcement_mode": "audit",
            },
        )
    )
    out = capsys.readouterr().err
    assert "HOOK: after_model" in out
    assert "rules=5" in out
    assert "matched=1" in out
    assert "action=ALLOW" in out


def test_emit_hook_summary_audit_mode_would_deny_marker(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """In AUDIT mode a DENY action is annotated as 'would deny'.

    Without this, operators reading the console would think a deny
    actually fired when the runtime only audited it.
    """
    sink = ConsoleAuditSink(verbose=False)
    sink.emit(
        AuditEvent(
            event_type=EventType.HOOK_END,
            hook="before_model",
            data={
                "total_rules": 1,
                "matched_rules": 1,
                "final_action": "deny",
                "enforcement_mode": "audit",
            },
        )
    )
    out = capsys.readouterr().err
    assert "AUDIT (would deny)" in out


def test_emit_hook_summary_enforce_mode_deny_not_annotated(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """In ENFORCE mode the 'would deny' annotation is NOT applied."""
    sink = ConsoleAuditSink(verbose=False)
    sink.emit(
        AuditEvent(
            event_type=EventType.HOOK_END,
            hook="before_model",
            data={
                "total_rules": 1,
                "matched_rules": 1,
                "final_action": "deny",
                "enforcement_mode": "enforce",
            },
        )
    )
    out = capsys.readouterr().err
    assert "would deny" not in out
    assert "action=DENY" in out


# ---------------------------------------------------------------------------
# Session start / end
# ---------------------------------------------------------------------------


def test_emit_session_start_includes_packs_and_mode(
    capsys: pytest.CaptureFixture[str],
) -> None:
    sink = ConsoleAuditSink(verbose=False)
    sink.emit(
        AuditEvent(
            event_type=EventType.SESSION_START,
            agent_name="my-agent",
            data={"packs": ["iso42001", "owasp"], "enforcement_mode": "audit"},
        )
    )
    out = capsys.readouterr().err
    assert "Session started" in out
    assert "agent=my-agent" in out
    assert "iso42001,owasp" in out
    assert "mode=audit" in out


def test_emit_session_end_counters(capsys: pytest.CaptureFixture[str]) -> None:
    sink = ConsoleAuditSink(verbose=False)
    sink.emit(
        AuditEvent(
            event_type=EventType.SESSION_END,
            trace_id="trace-abc",
            data={
                "total_evaluations": 12,
                "rules_matched": 3,
                "rules_denied": 1,
            },
        )
    )
    out = capsys.readouterr().err
    assert "Session ended" in out
    assert "evaluations=12" in out
    assert "matched=3" in out
    assert "denied=1" in out


# ---------------------------------------------------------------------------
# Generic / fallback
# ---------------------------------------------------------------------------


def test_emit_generic_unknown_event_type(capsys: pytest.CaptureFixture[str]) -> None:
    """Anything that isn't a known event type falls through to _emit_generic.

    The generic formatter serializes ``data`` as JSON so operators can
    still inspect the payload even for events the sink doesn't know about.
    """
    sink = ConsoleAuditSink(verbose=True)
    sink.emit(
        AuditEvent(
            event_type="custom_event",
            agent_name="x",
            data={"foo": "bar", "n": 1},
        )
    )
    out = capsys.readouterr().err
    assert "custom_event" in out
    assert "x" in out
    assert '"foo": "bar"' in out
    assert '"n": 1' in out
