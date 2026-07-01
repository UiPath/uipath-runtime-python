"""Tests for trace-span verbosity / status semantics.

``TracesAuditSink`` emits an OpenTelemetry span for every governance
hook end and every rule evaluation. The verdict is split into
``evaluator_result`` (what the rule decided, mode-independent) and
``action_applied`` (what actually happened, derived from
evaluator_result + mode).

Mode travels with the event (set by the emitter from its
per-instance ``EnforcementMode``) so parallel runtimes running
different modes don't cross-contaminate the sink's view.

- ``verbosityLevel = 4`` (Error) and ``StatusCode.ERROR`` fire **only**
  when ``action_applied = DENY`` — i.e. the runtime actually blocked
  the agent (ENFORCE mode + configured action ``deny``).
- ``verbosityLevel = 3`` (Warning) and ``Status.UNSET`` for advisory
  outcomes (``action_applied`` in ``{AUDIT, HITL}``). HITL is its own
  bucket — escalation pauses for human review, it doesn't fail the
  run, so it stays Warning even in ENFORCE mode.
- Hook spans never set Status, regardless of mode or final_action.
  They're summary containers; severity belongs on the per-rule span.
- ``ALLOW`` / ``NONE`` results leave verbosityLevel unset (consumers
  apply their default) and never call set_status.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from uipath.core.governance import EnforcementMode

from uipath.runtime.governance._audit.base import AuditEvent, EventType
from uipath.runtime.governance._audit.traces import TracesAuditSink


@pytest.fixture
def captured_span(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Wire ``TracesAuditSink`` to a mock tracer and return the span mock."""
    span = MagicMock(name="span")
    tracer = MagicMock(name="tracer")
    tracer.start_as_current_span.return_value.__enter__.return_value = span
    tracer.start_as_current_span.return_value.__exit__.return_value = False
    monkeypatch.setattr(TracesAuditSink, "_get_tracer", lambda self: tracer)
    return span


def _hook_event(final_action: str, mode: EnforcementMode) -> AuditEvent:
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


def _rule_event(
    matched: bool, action: str, mode: EnforcementMode = EnforcementMode.AUDIT
) -> AuditEvent:
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
            "enforcement_mode": mode,
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
        ("deny", EnforcementMode.ENFORCE),
        ("deny", EnforcementMode.AUDIT),
        ("audit", EnforcementMode.AUDIT),
        ("escalate", EnforcementMode.AUDIT),
        ("allow", EnforcementMode.AUDIT),
    ],
)
def test_hook_span_never_sets_error(
    captured_span: MagicMock, final_action: str, mode: EnforcementMode
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
    sink = TracesAuditSink()
    sink.emit(_rule_event(matched=True, action="deny", mode=EnforcementMode.ENFORCE))

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
    description = status_arg.description or ""
    assert "commitment-language" in description
    assert "deny" in description


def test_enforce_mode_escalate_is_hitl_warning(captured_span: MagicMock) -> None:
    """Enforce mode + action=escalate = HITL pause, not a block.

    HITL is its own spec bucket distinct from DENY — escalation pauses
    for human review, the run isn't failed. So verbosityLevel stays at
    Warning and Status is not marked ERROR.
    """
    sink = TracesAuditSink()
    sink.emit(
        _rule_event(matched=True, action="escalate", mode=EnforcementMode.ENFORCE)
    )

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
    sink = TracesAuditSink()
    sink.emit(_rule_event(matched=True, action=action, mode=EnforcementMode.AUDIT))

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
    sink = TracesAuditSink()
    sink.emit(_rule_event(matched=True, action="audit", mode=EnforcementMode.ENFORCE))

    attrs = _span_attrs(captured_span)
    assert attrs.get("verbosityLevel") == 3
    assert attrs.get("uipath_governance.evaluator_result") == "DENY"
    assert attrs.get("uipath_governance.action_applied") == "AUDIT"
    assert attrs.get("uipath_governance.mode") == "ENFORCE"
    assert not captured_span.set_status.called


# ---------------------------------------------------------------------------
# Rule span — no violation, no verbosityLevel attribute (Orchestrator default = 2)
# ---------------------------------------------------------------------------


def test_unmatched_rule_no_verbosity_no_error(captured_span: MagicMock) -> None:
    """Unmatched evaluations → evaluator_result=ALLOW, action_applied=NONE, quiet."""
    sink = TracesAuditSink()
    sink.emit(_rule_event(matched=False, action="deny", mode=EnforcementMode.ENFORCE))

    attrs = _span_attrs(captured_span)
    assert "verbosityLevel" not in attrs
    assert attrs.get("uipath_governance.evaluator_result") == "ALLOW"
    assert attrs.get("uipath_governance.action_applied") == "NONE"
    assert not captured_span.set_status.called


def test_matched_allow_action_no_verbosity(captured_span: MagicMock) -> None:
    """A rule whose action is 'allow' is an explicit non-violation."""
    sink = TracesAuditSink()
    sink.emit(_rule_event(matched=True, action="allow", mode=EnforcementMode.ENFORCE))

    attrs = _span_attrs(captured_span)
    assert "verbosityLevel" not in attrs
    assert attrs.get("uipath_governance.evaluator_result") == "ALLOW"
    assert attrs.get("uipath_governance.action_applied") == "NONE"
    assert not captured_span.set_status.called


# ---------------------------------------------------------------------------
# Cross-runtime isolation — the architectural motivation for the refactor
# ---------------------------------------------------------------------------


def test_two_events_carry_independent_modes(captured_span: MagicMock) -> None:
    """Parallel runtimes (different modes) cannot cross-contaminate the sink.

    Mode travels on each event (set by the emitter from its own
    per-instance ``EnforcementMode``), so two consecutive emits with
    different modes each render their own correct
    ``uipath_governance.mode`` value — no shared state in the sink
    that one runtime could clobber for another.
    """
    sink = TracesAuditSink()

    sink.emit(_rule_event(matched=True, action="deny", mode=EnforcementMode.ENFORCE))
    sink.emit(_rule_event(matched=True, action="deny", mode=EnforcementMode.AUDIT))

    # Collect every set_attribute call ordered by emit.
    calls = [c.args for c in captured_span.set_attribute.call_args_list]
    modes = [v for k, v in calls if k == "uipath_governance.mode"]
    actions_applied = [v for k, v in calls if k == "uipath_governance.action_applied"]
    assert modes == ["ENFORCE", "AUDIT"]
    assert actions_applied == ["DENY", "AUDIT"]


# ---------------------------------------------------------------------------
# _get_tracer — deferred init + ImportError fallback
# ---------------------------------------------------------------------------


def test_get_tracer_initializes_lazily_and_caches() -> None:
    """``_get_tracer`` should populate ``self._tracer`` on first call and
    return the same tracer on subsequent calls. This exercises the
    real (non-monkeypatched) path — most other tests stub the method.
    """
    sink = TracesAuditSink()
    # Before first call — deferred, no tracer yet.
    assert sink._tracer is None

    tracer_1 = sink._get_tracer()
    assert tracer_1 is not None  # OTel is installed; a tracer is returned

    tracer_2 = sink._get_tracer()
    # Second call returns the cached tracer — no re-init log flood.
    assert tracer_2 is tracer_1


def test_get_tracer_returns_none_when_opentelemetry_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a downstream host strips OpenTelemetry, ``_get_tracer`` logs a
    warning once and caches ``False`` so subsequent calls short-circuit
    instead of retrying the import every event.
    """
    import builtins

    real_import = builtins.__import__

    def _blocked_import(name: str, *args, **kwargs):
        if name == "opentelemetry" or name.startswith("opentelemetry."):
            raise ImportError(f"simulated missing: {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)

    sink = TracesAuditSink()

    assert sink._get_tracer() is None
    # Cached — no retry.
    assert sink._tracer is False
    assert sink._get_tracer() is None


def test_emit_is_a_noop_when_tracer_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``_get_tracer`` returns None, ``emit`` short-circuits inside
    both ``_emit_rule_span`` and ``_emit_hook_span`` without touching
    the tracer. Regression guard for the early-return branch.
    """
    sink = TracesAuditSink()
    monkeypatch.setattr(sink, "_get_tracer", lambda: None)

    # Both event types — must not raise, must not create spans.
    sink.emit(_hook_event(final_action="deny", mode=EnforcementMode.ENFORCE))
    sink.emit(_rule_event(matched=True, action="deny", mode=EnforcementMode.ENFORCE))
    assert sink.spans_created == 0


# ---------------------------------------------------------------------------
# spans_created — internal counter
# ---------------------------------------------------------------------------


def test_spans_created_increments_per_successful_emit(
    captured_span: MagicMock,
) -> None:
    """The ``spans_created`` counter must reflect every successful span
    creation. Consumers use this for smoke-test assertions in
    integration tests.
    """
    sink = TracesAuditSink()

    assert sink.spans_created == 0
    sink.emit(_rule_event(matched=True, action="deny", mode=EnforcementMode.ENFORCE))
    assert sink.spans_created == 1
    sink.emit(_hook_event(final_action="deny", mode=EnforcementMode.ENFORCE))
    assert sink.spans_created == 2


# ---------------------------------------------------------------------------
# _resolve_mode — enforcement-mode field parsing
# ---------------------------------------------------------------------------


def test_resolve_mode_reads_string_field(captured_span: MagicMock) -> None:
    """Emitters that forward the raw enum value as a lowercase string
    (rather than the ``EnforcementMode`` object) must still resolve to
    the correct mode. This exercises the ``isinstance(mode, str)``
    branch of ``_resolve_mode``.
    """
    sink = TracesAuditSink()

    ev = AuditEvent(
        event_type=EventType.RULE_EVALUATION,
        agent_name="agent",
        hook="after_model",
        data={
            "policy_id": "A.1.1",
            "matched": True,
            "action": "deny",
            "enforcement_mode": "enforce",  # string, not EnforcementMode
        },
    )
    sink.emit(ev)

    modes = [
        c.args[1]
        for c in captured_span.set_attribute.call_args_list
        if c.args[0] == "uipath_governance.mode"
    ]
    assert modes == ["ENFORCE"]


def test_resolve_mode_falls_back_to_audit_on_unknown_string(
    captured_span: MagicMock,
) -> None:
    """An unparseable mode string (contract violation) must not crash
    the sink — it falls back to AUDIT so a bad emitter can't kill
    governance telemetry.
    """
    sink = TracesAuditSink()

    ev = AuditEvent(
        event_type=EventType.RULE_EVALUATION,
        agent_name="agent",
        hook="after_model",
        data={
            "policy_id": "A.1.1",
            "matched": True,
            "action": "deny",
            "enforcement_mode": "not-a-real-mode",
        },
    )
    sink.emit(ev)

    modes = [
        c.args[1]
        for c in captured_span.set_attribute.call_args_list
        if c.args[0] == "uipath_governance.mode"
    ]
    assert modes == ["AUDIT"]  # safe default


# ---------------------------------------------------------------------------
# _package_version — install-metadata lookup + fallback
# ---------------------------------------------------------------------------


def test_package_version_returns_unknown_when_package_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The version helper must degrade gracefully when the
    ``uipath-runtime`` package metadata isn't findable (e.g., when
    running from a source checkout without an install).
    """
    import importlib.metadata

    from uipath.runtime.governance._audit import traces

    def _raise_not_found(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", _raise_not_found)

    assert traces._package_version() == "unknown"


# ---------------------------------------------------------------------------
# Exception isolation — sink swallows internal errors, per contract
# ---------------------------------------------------------------------------


def test_rule_span_exception_is_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sink emit must never propagate exceptions to the caller — the
    audit manager's circuit breaker tracks failures, but the sink
    itself catches so a broken tracer can't crash the agent hook.
    """
    sink = TracesAuditSink()

    # Tracer whose start_as_current_span raises inside the with-block.
    tracer = MagicMock()
    tracer.start_as_current_span.side_effect = RuntimeError("tracer boom")
    monkeypatch.setattr(sink, "_get_tracer", lambda: tracer)

    # Must not raise.
    sink.emit(_rule_event(matched=True, action="deny", mode=EnforcementMode.ENFORCE))
    sink.emit(_hook_event(final_action="deny", mode=EnforcementMode.ENFORCE))

    # And no span was successfully created.
    assert sink.spans_created == 0
