"""Tests that the evaluator passes the new telemetry fields downstream.

The evaluator must:

- Measure per-rule and per-hook wall-clock duration.
- Collect disabled rule ids into ``skipped_policy_names``.
- Count matched UiPath-mapped guardrails (``guardrail_dispatched_count``).
- Hand all of the above to ``AuditManager.emit_rule_evaluation`` and
  ``emit_hook_summary`` so the ``TrackEventAuditSink`` payload carries
  them.

We capture events via a manager registered with no default sinks +
one ``_CapturingSink`` so the assertions see exactly the per-rule and
hook-summary events the evaluator emits.
"""

from __future__ import annotations

import pytest
from uipath.core.governance import EnforcementMode
from uipath.core.governance.models import Action, LifecycleHook

from uipath.runtime.governance._audit.base import (
    AuditEvent,
    AuditManager,
    AuditSink,
    EventType,
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


class _CapturingSink(AuditSink):
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    @property
    def name(self) -> str:
        return "capturing"

    def emit(self, event: AuditEvent) -> None:
        self.events.append(event)


def _rule(
    rule_id: str,
    *,
    enabled: bool = True,
    matches: bool = True,
    action: Action = Action.DENY,
) -> Rule:
    """A rule whose ``contains`` check always (or never) matches the input."""
    return Rule(
        rule_id=rule_id,
        name=f"rule-{rule_id}",
        clause=rule_id,
        hook=LifecycleHook.BEFORE_AGENT,
        action=action,
        enabled=enabled,
        checks=[
            Check(
                conditions=[
                    Condition(
                        operator="contains",
                        field="agent_input",
                        value="needle" if matches else "absent-needle",
                    )
                ],
                action=action,
                message=f"matched {rule_id}",
            )
        ],
    )


def _pack(*rules: Rule) -> PolicyIndex:
    idx = PolicyIndex()
    idx.add_pack(
        PolicyPack(name="test_pack", version="1.0", description="t", rules=list(rules))
    )
    return idx


def _ctx() -> CheckContext:
    return CheckContext(
        hook=LifecycleHook.BEFORE_AGENT,
        agent_name="agent-x",
        runtime_id="run-1",
        agent_input="needle",
    )


@pytest.fixture
def sink_and_manager() -> tuple[_CapturingSink, AuditManager]:
    """Sink-capturing manager with no default sinks (no traces, no track_events)."""
    sink = _CapturingSink()
    mgr = AuditManager(register_default_sinks=False)
    mgr.register_sink(sink)
    return sink, mgr


def _rule_events(sink: _CapturingSink) -> list[AuditEvent]:
    return [e for e in sink.events if e.event_type == EventType.RULE_EVALUATION]


def _hook_summary(sink: _CapturingSink) -> AuditEvent:
    summaries = [e for e in sink.events if e.event_type == EventType.HOOK_END]
    assert len(summaries) == 1, f"expected 1 hook summary, got {len(summaries)}"
    return summaries[0]


# ---------------------------------------------------------------------------
# Per-rule timing
# ---------------------------------------------------------------------------


def test_rule_evaluation_carries_duration_ms(
    sink_and_manager: tuple[_CapturingSink, AuditManager],
) -> None:
    sink, mgr = sink_and_manager
    try:
        evaluator = GovernanceEvaluator(
            _pack(_rule("A.1.1")),
            enforcement_mode=EnforcementMode.AUDIT,
            audit_manager=mgr,
        )
        evaluator.evaluate(_ctx())
    finally:
        mgr.close()

    rule_evs = _rule_events(sink)
    assert len(rule_evs) == 1
    duration = rule_evs[0].data["duration_ms"]
    assert isinstance(duration, float)
    assert duration >= 0.0


def test_rule_evaluation_mapped_to_uipath_is_false_for_native(
    sink_and_manager: tuple[_CapturingSink, AuditManager],
) -> None:
    """Native rules (no guardrail_fallback) always emit ``mapped_to_uipath=False``."""
    sink, mgr = sink_and_manager
    try:
        evaluator = GovernanceEvaluator(
            _pack(_rule("A.1.1")),
            enforcement_mode=EnforcementMode.AUDIT,
            audit_manager=mgr,
        )
        evaluator.evaluate(_ctx())
    finally:
        mgr.close()

    rule_evs = _rule_events(sink)
    assert rule_evs[0].data["mapped_to_uipath"] is False


# ---------------------------------------------------------------------------
# Hook-summary aggregates
# ---------------------------------------------------------------------------


def test_hook_summary_carries_total_duration(
    sink_and_manager: tuple[_CapturingSink, AuditManager],
) -> None:
    sink, mgr = sink_and_manager
    try:
        evaluator = GovernanceEvaluator(
            _pack(_rule("A.1.1"), _rule("A.1.2", matches=False)),
            enforcement_mode=EnforcementMode.AUDIT,
            audit_manager=mgr,
        )
        evaluator.evaluate(_ctx())
    finally:
        mgr.close()

    summary = _hook_summary(sink)
    duration = summary.data["duration_ms"]
    assert isinstance(duration, float)
    assert duration >= 0.0


def test_hook_summary_tracks_skipped_disabled_rules(
    sink_and_manager: tuple[_CapturingSink, AuditManager],
) -> None:
    """Disabled rules appear in ``skipped_policy_names`` (and skipped_count)."""
    sink, mgr = sink_and_manager
    try:
        evaluator = GovernanceEvaluator(
            _pack(
                _rule("A.1.1"),                      # enabled, matches
                _rule("A.1.2", enabled=False),       # DISABLED
                _rule("A.1.3", enabled=False),       # DISABLED
            ),
            enforcement_mode=EnforcementMode.AUDIT,
            audit_manager=mgr,
        )
        evaluator.evaluate(_ctx())
    finally:
        mgr.close()

    summary = _hook_summary(sink)
    assert summary.data["skipped_count"] == 2
    assert set(summary.data["skipped_policy_names"]) == {"A.1.2", "A.1.3"}


def test_hook_summary_passed_and_denied_counts(
    sink_and_manager: tuple[_CapturingSink, AuditManager],
) -> None:
    """One match + two non-matches → denied=1, passed=2, matched_rules=1."""
    sink, mgr = sink_and_manager
    try:
        evaluator = GovernanceEvaluator(
            _pack(
                _rule("A.1.1"),                          # matches (DENY)
                _rule("A.1.2", matches=False),           # passes
                _rule("A.1.3", matches=False),           # passes
            ),
            enforcement_mode=EnforcementMode.AUDIT,
            audit_manager=mgr,
        )
        evaluator.evaluate(_ctx())
    finally:
        mgr.close()

    summary = _hook_summary(sink)
    assert summary.data["denied_count"] == 1
    assert summary.data["passed_count"] == 2
    # ``matched_rules`` keeps its historical "any check matched" sense.
    assert summary.data["matched_rules"] == 1


def test_hook_summary_matched_allow_rule_counts_as_passed(
    sink_and_manager: tuple[_CapturingSink, AuditManager],
) -> None:
    """A matched rule with action=ALLOW is a positive informational match.

    It should NOT contribute to ``denied_count`` — it rolls into
    ``passed_count`` instead. ``matched_rules`` (raw count) still
    includes it for backward compatibility with the legacy traces sink.
    """
    sink, mgr = sink_and_manager
    try:
        evaluator = GovernanceEvaluator(
            _pack(
                _rule("A.1.1"),                                # matches (DENY)
                _rule("A.1.2", action=Action.ALLOW),           # matches (ALLOW) — positive
                _rule("A.1.3", matches=False),                 # passes (unmatched)
            ),
            enforcement_mode=EnforcementMode.AUDIT,
            audit_manager=mgr,
        )
        evaluator.evaluate(_ctx())
    finally:
        mgr.close()

    summary = _hook_summary(sink)
    # 3 total rules; only the explicit DENY counts as a denial.
    assert summary.data["denied_count"] == 1
    # ``passed_count`` includes the unmatched rule AND the matched-allow rule.
    assert summary.data["passed_count"] == 2
    # Raw ``matched_rules`` still reflects "any check matched" — both
    # the DENY and the ALLOW match contribute.
    assert summary.data["matched_rules"] == 2


def test_hook_summary_guardrail_dispatched_count_for_native_rules_is_zero(
    sink_and_manager: tuple[_CapturingSink, AuditManager],
) -> None:
    """Without guardrail_fallback conditions, dispatched count is 0."""
    sink, mgr = sink_and_manager
    try:
        evaluator = GovernanceEvaluator(
            _pack(_rule("A.1.1")),
            enforcement_mode=EnforcementMode.AUDIT,
            audit_manager=mgr,
        )
        evaluator.evaluate(_ctx())
    finally:
        mgr.close()

    summary = _hook_summary(sink)
    assert summary.data["guardrail_dispatched_count"] == 0
