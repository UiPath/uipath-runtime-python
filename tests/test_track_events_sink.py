"""Tests for :class:`TrackEventAuditSink`.

Verifies the volume-control filter (``accepts``), event-name vocabulary,
payload shape (runtime metadata + per-event fields), and OTel-span →
``operation_id`` correlation (sink resolves the correlation id from
the live OTel context at emit time, not from the event itself). Uses
a capture-callable in place of the host-supplied ``track_event`` so
no I/O fires.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from uipath.core.governance import EnforcementMode

from uipath.runtime.governance._audit.base import AuditEvent, EventType
from uipath.runtime.governance._audit.metadata import GovernanceRuntimeMetadata
from uipath.runtime.governance._audit.track_events import (
    EVENT_HOOK_SUMMARY,
    EVENT_RULE_DENIED,
    TrackEventAuditSink,
)


class _Capture:
    """Stand-in for ``provider.track_event`` — records every call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        *,
        event_name: str,
        data: dict[str, Any] | None = None,
        operation_id: str | None = None,
    ) -> None:
        self.calls.append(
            {"event_name": event_name, "data": data, "operation_id": operation_id}
        )


@pytest.fixture
def capture() -> _Capture:
    return _Capture()


@pytest.fixture
def metadata() -> GovernanceRuntimeMetadata:
    return GovernanceRuntimeMetadata(
        execution_engine="uipath_native_governance_checker",
        agent_type="uipath_coded",
        agent_framework="langchain",
        runtime_version="9.9.9-test",
    )


@pytest.fixture
def sink(capture: _Capture, metadata: GovernanceRuntimeMetadata) -> TrackEventAuditSink:
    return TrackEventAuditSink(capture, metadata)


def _rule_event(
    *,
    matched: bool,
    action: str = "deny",
    mode: EnforcementMode = EnforcementMode.AUDIT,
    duration_ms: float = 12.5,
    mapped_to_uipath: bool = False,
) -> AuditEvent:
    return AuditEvent(
        event_type=EventType.RULE_EVALUATION,
        agent_name="agent-x",
        hook="after_model",
        data={
            "policy_id": "A.6.1.4",
            "rule_name": "commitment-language",
            "pack_name": "iso42001",
            "matched": matched,
            "action": action,
            "enforcement_mode": mode,
            "detail": "Detected a commitment phrase.",
            "duration_ms": duration_ms,
            "mapped_to_uipath": mapped_to_uipath,
        },
        timestamp=datetime(2026, 6, 25, 12, 0, 0, tzinfo=timezone.utc),
    )


def _hook_event(
    *,
    mode: EnforcementMode = EnforcementMode.AUDIT,
    duration_ms: float = 45.0,
    passed_count: int = 4,
    denied_count: int = 1,
    skipped_policy_names: list[str] | None = None,
    guardrail_dispatched_count: int = 0,
) -> AuditEvent:
    return AuditEvent(
        event_type=EventType.HOOK_END,
        agent_name="agent-x",
        hook="after_model",
        data={
            "total_rules": passed_count + denied_count,
            "matched_rules": denied_count,
            "final_action": "audit",
            "enforcement_mode": mode,
            "duration_ms": duration_ms,
            "passed_count": passed_count,
            "denied_count": denied_count,
            "skipped_count": len(skipped_policy_names or []),
            "skipped_policy_names": list(skipped_policy_names or []),
            "guardrail_dispatched_count": guardrail_dispatched_count,
        },
        timestamp=datetime(2026, 6, 25, 12, 0, 0, tzinfo=timezone.utc),
    )


# ---------------------------------------------------------------------------
# accepts() — volume-control filter
# ---------------------------------------------------------------------------


def test_accepts_matched_rule_evaluation(sink: TrackEventAuditSink) -> None:
    assert sink.accepts(_rule_event(matched=True)) is True


def test_rejects_unmatched_rule_evaluation(sink: TrackEventAuditSink) -> None:
    assert sink.accepts(_rule_event(matched=False)) is False


def test_rejects_matched_allow_rule(sink: TrackEventAuditSink) -> None:
    """A matched rule with action=allow is a positive informational
    match — it should NOT trigger the ``rule.denied`` stream. It rolls
    into the hook summary's ``passed_count`` instead.
    """
    assert sink.accepts(_rule_event(matched=True, action="allow")) is False


def test_direct_emit_of_unmatched_rule_is_noop(
    sink: TrackEventAuditSink, capture: _Capture
) -> None:
    """Defense-in-depth: even if a caller bypasses ``accepts``, passed
    rules must not be routed to ``governance.rule.denied``.

    The AuditManager dispatch path always consults ``accepts`` first,
    but a direct ``sink.emit(...)`` call (tests, future alternate
    dispatch) must still produce zero ``track_event`` calls for a
    ``matched=False`` rule.
    """
    sink.emit(_rule_event(matched=False))
    assert capture.calls == []


def test_direct_emit_of_matched_allow_rule_is_noop(
    sink: TrackEventAuditSink, capture: _Capture
) -> None:
    """Defense-in-depth equivalent of the matched-but-allow filter."""
    sink.emit(_rule_event(matched=True, action="allow"))
    assert capture.calls == []


# ---------------------------------------------------------------------------
# DISABLED mode — zero telemetry across every event type
# ---------------------------------------------------------------------------


def test_rejects_disabled_mode_rule_evaluation(sink: TrackEventAuditSink) -> None:
    """Governance off → ``accepts`` returns False for rule events."""
    assert (
        sink.accepts(_rule_event(matched=True, mode=EnforcementMode.DISABLED)) is False
    )


def test_rejects_disabled_mode_hook_summary(sink: TrackEventAuditSink) -> None:
    """Governance off → ``accepts`` returns False for hook summaries."""
    assert sink.accepts(_hook_event(mode=EnforcementMode.DISABLED)) is False


def test_direct_emit_of_disabled_rule_is_noop(
    sink: TrackEventAuditSink, capture: _Capture
) -> None:
    """Defense-in-depth: ``emit`` drops DISABLED-mode events too."""
    sink.emit(_rule_event(matched=True, mode=EnforcementMode.DISABLED))
    assert capture.calls == []


def test_direct_emit_of_disabled_hook_is_noop(
    sink: TrackEventAuditSink, capture: _Capture
) -> None:
    """Defense-in-depth for hook summary in DISABLED mode."""
    sink.emit(_hook_event(mode=EnforcementMode.DISABLED))
    assert capture.calls == []


# ---------------------------------------------------------------------------
# Empty hook summary suppression
# ---------------------------------------------------------------------------


def test_rejects_empty_hook_summary(sink: TrackEventAuditSink) -> None:
    """``total_rules=0`` AND ``skipped_count=0`` → nothing to report."""
    ev = _hook_event(passed_count=0, denied_count=0, skipped_policy_names=[])
    assert sink.accepts(ev) is False


def test_accepts_hook_summary_with_only_skipped(sink: TrackEventAuditSink) -> None:
    """Hook with ``total=0`` but ``skipped_count>0`` is still operator-useful."""
    ev = _hook_event(
        passed_count=0,
        denied_count=0,
        skipped_policy_names=["A.1.1", "A.1.2"],
    )
    assert sink.accepts(ev) is True


def test_direct_emit_of_empty_hook_is_noop(
    sink: TrackEventAuditSink, capture: _Capture
) -> None:
    """Defense-in-depth equivalent of the empty-hook filter."""
    sink.emit(_hook_event(passed_count=0, denied_count=0, skipped_policy_names=[]))
    assert capture.calls == []


def test_accepts_hook_end(sink: TrackEventAuditSink) -> None:
    assert sink.accepts(_hook_event()) is True


def test_rejects_other_event_types(sink: TrackEventAuditSink) -> None:
    for et in (
        EventType.HOOK_START,
        EventType.SESSION_START,
        EventType.SESSION_END,
        EventType.POLICY_VIOLATION,
        EventType.POLICY_ALLOW,
        EventType.PACKS_LOADED,
    ):
        ev = AuditEvent(event_type=et, agent_name="agent-x")
        assert sink.accepts(ev) is False, f"sink must drop {et}"


# ---------------------------------------------------------------------------
# emit() — rule-denied event shape
# ---------------------------------------------------------------------------


def test_rule_denied_event_name_and_operation_id(
    sink: TrackEventAuditSink, capture: _Capture
) -> None:
    """``operation_id`` is the live OTel span's trace id rendered as 32-hex.

    The sink reads from :func:`opentelemetry.trace.get_current_span` at
    emit time — the audit manager dispatches synchronously on the
    caller's thread, so any span open on the calling thread is visible
    to the sink directly.
    """
    from opentelemetry.sdk.trace import TracerProvider

    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("agent-run") as span:
        expected = f"{span.get_span_context().trace_id:032x}"
        sink.emit(_rule_event(matched=True))

    assert len(capture.calls) == 1
    call = capture.calls[0]
    assert call["event_name"] == EVENT_RULE_DENIED
    assert call["operation_id"] == expected


def test_rule_denied_payload_carries_metadata(
    sink: TrackEventAuditSink, capture: _Capture
) -> None:
    sink.emit(_rule_event(matched=True))
    data = capture.calls[0]["data"]
    assert data["execution_engine"] == "uipath_native_governance_checker"
    assert data["agent_type"] == "uipath_coded"
    assert data["agent_framework"] == "langchain"
    assert data["runtime_version"] == "9.9.9-test"
    assert data["agent_name"] == "agent-x"
    assert data["hook"] == "after_model"


def test_rule_denied_payload_splits_pack_and_clause(
    sink: TrackEventAuditSink, capture: _Capture
) -> None:
    """``pack`` and ``clause`` are separate fields so consumers can aggregate."""
    sink.emit(_rule_event(matched=True))
    data = capture.calls[0]["data"]
    assert data["pack"] == "iso42001"
    assert data["clause"] == "A.6.1.4"
    assert data["rule_name"] == "commitment-language"


def test_rule_denied_audit_mode_collapses_to_audit(
    sink: TrackEventAuditSink, capture: _Capture
) -> None:
    """AUDIT mode never escalates DENY past AUDIT, but evaluator_result is true."""
    sink.emit(_rule_event(matched=True, action="deny", mode=EnforcementMode.AUDIT))
    data = capture.calls[0]["data"]
    assert data["mode"] == "AUDIT"
    assert data["evaluator_result"] == "DENY"
    assert data["action_applied"] == "AUDIT"


def test_rule_denied_enforce_mode_deny(
    sink: TrackEventAuditSink, capture: _Capture
) -> None:
    sink.emit(_rule_event(matched=True, action="deny", mode=EnforcementMode.ENFORCE))
    data = capture.calls[0]["data"]
    assert data["mode"] == "ENFORCE"
    assert data["evaluator_result"] == "DENY"
    assert data["action_applied"] == "DENY"


def test_rule_denied_enforce_mode_escalate_is_hitl(
    sink: TrackEventAuditSink, capture: _Capture
) -> None:
    sink.emit(
        _rule_event(matched=True, action="escalate", mode=EnforcementMode.ENFORCE)
    )
    data = capture.calls[0]["data"]
    assert data["evaluator_result"] == "HITL"
    assert data["action_applied"] == "HITL"


def test_rule_denied_carries_duration(
    sink: TrackEventAuditSink, capture: _Capture
) -> None:
    sink.emit(_rule_event(matched=True, duration_ms=42.5))
    assert capture.calls[0]["data"]["duration_ms"] == 42.5


def test_rule_denied_mapped_to_uipath_passes_through(
    sink: TrackEventAuditSink, capture: _Capture
) -> None:
    """Field defaults False for native events but the schema is stable."""
    sink.emit(_rule_event(matched=True, mapped_to_uipath=False))
    assert capture.calls[0]["data"]["mapped_to_uipath"] is False


# ---------------------------------------------------------------------------
# emit() — hook-summary event shape
# ---------------------------------------------------------------------------


def test_hook_summary_event_name_and_operation_id(
    sink: TrackEventAuditSink, capture: _Capture
) -> None:
    """Same OTel-derived operation_id contract as the rule-denied event."""
    from opentelemetry.sdk.trace import TracerProvider

    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("agent-run") as span:
        expected = f"{span.get_span_context().trace_id:032x}"
        sink.emit(_hook_event())

    assert len(capture.calls) == 1
    assert capture.calls[0]["event_name"] == EVENT_HOOK_SUMMARY
    assert capture.calls[0]["operation_id"] == expected


def test_hook_summary_carries_counts(
    sink: TrackEventAuditSink, capture: _Capture
) -> None:
    sink.emit(
        _hook_event(
            passed_count=10,
            denied_count=2,
            skipped_policy_names=["A.1.2", "A.3.4"],
            guardrail_dispatched_count=3,
        )
    )
    data = capture.calls[0]["data"]
    assert data["passed_count"] == 10
    assert data["denied_count"] == 2
    assert data["skipped_count"] == 2
    assert data["skipped_policy_names"] == ["A.1.2", "A.3.4"]
    assert data["guardrail_dispatched_count"] == 3


def test_hook_summary_duration_ms(sink: TrackEventAuditSink, capture: _Capture) -> None:
    sink.emit(_hook_event(duration_ms=123.4))
    assert capture.calls[0]["data"]["duration_ms"] == 123.4


def test_hook_summary_carries_metadata(
    sink: TrackEventAuditSink, capture: _Capture
) -> None:
    """Same per-runtime metadata stamping as rule-denied events."""
    sink.emit(_hook_event())
    data = capture.calls[0]["data"]
    assert data["execution_engine"] == "uipath_native_governance_checker"
    assert data["agent_type"] == "uipath_coded"
    assert data["agent_framework"] == "langchain"
    assert data["runtime_version"] == "9.9.9-test"


def test_hook_summary_mode_string(sink: TrackEventAuditSink, capture: _Capture) -> None:
    sink.emit(_hook_event(mode=EnforcementMode.ENFORCE))
    assert capture.calls[0]["data"]["mode"] == "ENFORCE"


# ---------------------------------------------------------------------------
# operation_id fallback when no OTel span is active
# ---------------------------------------------------------------------------


def test_operation_id_none_when_no_active_span(
    sink: TrackEventAuditSink, capture: _Capture
) -> None:
    """No live OTel span → ``operation_id=None`` (consumer fills in its own)."""
    sink.emit(_rule_event(matched=True))
    assert capture.calls[0]["operation_id"] is None
