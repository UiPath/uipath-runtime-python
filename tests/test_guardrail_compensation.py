"""Tests for compensating governance calls to /runtime/govern.

The runtime layer owns only the bounded background pool and the
trace-id capture; HTTP/auth/URL/header concerns live behind the
:class:`uipath.core.governance.GovernanceCompensationProvider` protocol
and are exercised in ``uipath-platform``'s own tests.

These tests cover:

- ``disabled_guardrails`` — distilling fired ``guardrail_fallback`` rules
  into per-rule wire metadata.
- ``submit_compensation`` — pool routing, in-flight backpressure,
  shutdown safety, wire-model assembly, and the thread-boundary
  trace-id capture.
- ``_resolve_trace_id`` — env > live OTel span > fallback ordering.
- Evaluator integration is guarded by ``importorskip`` because the
  evaluator module isn't present on this branch yet; when it lands,
  the dispatch tests need to be rewritten for the new
  ``provider``-first signature.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from uipath.core.governance import (
    FiredRule,
    GovernanceCompensationProvider,
    GovernRequest,
)
from uipath.core.governance.models import Action, LifecycleHook

from tests._helpers import reset_enforcement_mode
from uipath.runtime.governance.config import (
    EnforcementMode,
    set_enforcement_mode,
)
from uipath.runtime.governance.native import guardrail_compensation
from uipath.runtime.governance.native.guardrail_compensation import (
    _resolve_trace_id,
    disabled_guardrails,
    submit_compensation,
)
from uipath.runtime.governance.native.models import (
    Check,
    CheckContext,
    Condition,
    PolicyIndex,
    PolicyPack,
    Rule,
)

# The evaluator wiring (which injects the provider and calls
# ``submit_compensation``) is not present on this branch yet. Tests that
# need it are skipped until the module lands; when it does, they must be
# rewritten because the function signature changed (``provider`` is now
# positional-first).
try:
    from uipath.runtime.governance.native.evaluator import (  # type: ignore[import-not-found]
        GovernanceEvaluator,
    )

    _HAS_EVALUATOR = True
except ImportError:
    _HAS_EVALUATOR = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _provider() -> MagicMock:
    """Mock satisfying the GovernanceCompensationProvider protocol."""
    return MagicMock(spec=GovernanceCompensationProvider)


def _rules(
    *validators: str,
    rule_id: str = "R1",
    rule_name: str = "n",
    pack: str = "p",
) -> list[FiredRule]:
    """Build a list of FiredRule wire models — one per validator."""
    return [
        FiredRule(
            rule_id=rule_id,
            rule_name=rule_name,
            pack_name=pack,
            validator=v,
        )
        for v in validators
    ]


@pytest.fixture(autouse=True)
def _reset_enforcement_mode() -> Any:
    reset_enforcement_mode()
    yield
    reset_enforcement_mode()


# ---------------------------------------------------------------------------
# disabled_guardrails
# ---------------------------------------------------------------------------


def test_disabled_guardrails_returns_fired_rule_for_matched_disabled_guardrail() -> None:
    cond = SimpleNamespace(
        operator="guardrail_fallback",
        value={
            "validator": "pii_detection",
            "mapped_to_uipath": True,
            "policy_enabled": False,
        },
    )
    rule = SimpleNamespace(checks=[SimpleNamespace(conditions=[cond])], pack_name="")
    audit = SimpleNamespace(
        evaluations=[
            SimpleNamespace(matched=True, rule_id="R1", rule_name="PII guardrail")
        ]
    )
    policy_index = SimpleNamespace(
        get_rule=lambda rid: rule if rid == "R1" else None
    )

    out = disabled_guardrails(audit, policy_index)

    assert len(out) == 1
    fr = out[0]
    assert isinstance(fr, FiredRule)
    assert fr.rule_id == "R1"
    assert fr.rule_name == "PII guardrail"
    assert fr.pack_name == ""
    assert fr.validator == "pii_detection"


def test_disabled_guardrails_skips_unmatched_evaluations() -> None:
    audit = SimpleNamespace(
        evaluations=[SimpleNamespace(matched=False, rule_id="R1", rule_name="x")]
    )
    policy_index = SimpleNamespace(get_rule=lambda rid: None)
    assert disabled_guardrails(audit, policy_index) == []


def test_disabled_guardrails_skips_non_guardrail_conditions() -> None:
    cond = SimpleNamespace(operator="regex", value="some-pattern")
    rule = SimpleNamespace(checks=[SimpleNamespace(conditions=[cond])])
    audit = SimpleNamespace(
        evaluations=[SimpleNamespace(matched=True, rule_id="R1", rule_name="x")]
    )
    policy_index = SimpleNamespace(get_rule=lambda rid: rule)
    assert disabled_guardrails(audit, policy_index) == []


def test_disabled_guardrails_skips_enabled_guardrails() -> None:
    """If the guardrail is mapped to UiPath AND enabled, no compensation needed."""
    cond = SimpleNamespace(
        operator="guardrail_fallback",
        value={
            "validator": "pii_detection",
            "mapped_to_uipath": True,
            "policy_enabled": True,
        },
    )
    rule = SimpleNamespace(checks=[SimpleNamespace(conditions=[cond])], pack_name="")
    audit = SimpleNamespace(
        evaluations=[SimpleNamespace(matched=True, rule_id="R1", rule_name="x")]
    )
    policy_index = SimpleNamespace(get_rule=lambda rid: rule)
    assert disabled_guardrails(audit, policy_index) == []


def test_disabled_guardrails_skips_unmapped_guardrails() -> None:
    """If the guardrail isn't mapped to UiPath, server can't fall back for us."""
    cond = SimpleNamespace(
        operator="guardrail_fallback",
        value={
            "validator": "pii_detection",
            "mapped_to_uipath": False,
            "policy_enabled": False,
        },
    )
    rule = SimpleNamespace(checks=[SimpleNamespace(conditions=[cond])], pack_name="")
    audit = SimpleNamespace(
        evaluations=[SimpleNamespace(matched=True, rule_id="R1", rule_name="x")]
    )
    policy_index = SimpleNamespace(get_rule=lambda rid: rule)
    assert disabled_guardrails(audit, policy_index) == []


# ---------------------------------------------------------------------------
# submit_compensation — short-circuits + pool routing + backpressure
# ---------------------------------------------------------------------------


def test_submit_compensation_empty_rules_short_circuits() -> None:
    """No rules → no pool submit, no provider call."""
    provider = _provider()
    with patch.object(guardrail_compensation, "_pool") as mock_pool:
        submit_compensation(provider, [], {}, "before_model", "t", "ts", "a", "r")
    mock_pool.submit.assert_not_called()
    provider.compensate.assert_not_called()


def test_submit_compensation_no_validators_short_circuits() -> None:
    """Rules with empty validator strings → no call (nothing to dispatch)."""
    provider = _provider()
    rules = [FiredRule(rule_id="R", rule_name="n", pack_name="p", validator="")]
    with patch.object(guardrail_compensation, "_pool") as mock_pool:
        submit_compensation(provider, rules, {}, "before_model", "t", "ts", "a", "r")
    mock_pool.submit.assert_not_called()
    provider.compensate.assert_not_called()


def test_submit_compensation_routes_through_pool() -> None:
    """A non-empty rules list submits a single task to the pool."""
    provider = _provider()
    with patch.object(guardrail_compensation, "_pool") as mock_pool:
        submit_compensation(
            provider,
            _rules("pii_detection"),
            {"content": "x"},
            "before_model",
            "trace-1",
            "ts",
            "agent",
            "run",
        )
    mock_pool.submit.assert_called_once()


def test_submit_compensation_drops_when_pool_saturated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the in-flight semaphore is exhausted, the call is dropped."""
    drained = threading.BoundedSemaphore(1)
    drained.acquire()  # next acquire(blocking=False) returns False
    monkeypatch.setattr(guardrail_compensation, "_inflight", drained)

    provider = _provider()
    with patch.object(guardrail_compensation, "_pool") as mock_pool:
        submit_compensation(
            provider,
            _rules("pii_detection"),
            {},
            "before_model",
            "trace-1",
            "ts",
            "agent",
            "run",
        )

    mock_pool.submit.assert_not_called()
    provider.compensate.assert_not_called()


def test_submit_compensation_swallows_pool_shutdown_runtimeerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the pool was shut down at process exit, submit must not raise."""
    monkeypatch.setattr(
        guardrail_compensation, "_inflight", threading.BoundedSemaphore(4)
    )

    class _ShutdownPool:
        def submit(self, fn: Any, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("cannot schedule new futures after shutdown")

    monkeypatch.setattr(guardrail_compensation, "_pool", _ShutdownPool())

    # Must not raise.
    submit_compensation(
        _provider(), _rules("x"), {}, "before_model", "t", "ts", "a", "r"
    )


# ---------------------------------------------------------------------------
# submit_compensation — wire-model assembly + provider invocation
# ---------------------------------------------------------------------------


def _run_inline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``_pool.submit`` execute its task synchronously on the caller.

    Lets us assert provider behavior without leaning on a wait()/sleep().
    """

    def _sync_submit(fn: Any, *args: Any, **kwargs: Any) -> None:
        fn()

    monkeypatch.setattr(
        guardrail_compensation._pool, "submit", _sync_submit
    )


def test_submit_compensation_invokes_provider_with_govern_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The provider receives a GovernRequest carrying every wire field."""
    _run_inline(monkeypatch)
    provider = _provider()
    rules = _rules("pii_detection", "harmful_content")

    submit_compensation(
        provider,
        rules,
        {"content": "x"},
        "before_model",
        "trace-1",
        "2026-06-06T00:00:00Z",
        "langchain",
        "patch-langchain",
    )

    provider.compensate.assert_called_once()
    (request,) = provider.compensate.call_args.args
    assert isinstance(request, GovernRequest)
    # distinct validators drive the guardrail API call
    assert request.validators == ["pii_detection", "harmful_content"]
    assert request.rules == rules
    assert request.data == {"content": "x"}
    assert request.hook == "before_model"
    assert request.trace_id == "trace-1"
    assert request.src_timestamp == "2026-06-06T00:00:00Z"
    assert request.agent_name == "langchain"
    assert request.runtime_id == "patch-langchain"
    # Job-context fields are left for the provider to auto-fill from env.
    assert request.folder_key is None
    assert request.job_key is None
    assert request.process_key is None
    assert request.reference_id is None
    assert request.agent_version is None


def test_submit_compensation_dedupes_validators(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multiple rules with the same validator collapse on the wire."""
    _run_inline(monkeypatch)
    provider = _provider()
    rules = _rules("pii_detection") + _rules("pii_detection", rule_id="R2")

    submit_compensation(
        provider, rules, {}, "before_model", "t", "ts", "a", "r"
    )

    (request,) = provider.compensate.call_args.args
    assert request.validators == ["pii_detection"]
    # Per-rule metadata is preserved (one record per rule even with shared validator).
    assert len(request.rules) == 2


def test_submit_compensation_swallows_provider_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider exception must never propagate to the caller / agent."""
    _run_inline(monkeypatch)
    provider = _provider()
    provider.compensate.side_effect = RuntimeError("network down")

    # Must not raise.
    submit_compensation(
        provider, _rules("x"), {}, "before_model", "t", "ts", "a", "r"
    )

    provider.compensate.assert_called_once()


# ---------------------------------------------------------------------------
# _resolve_trace_id — must capture the live trace on the caller thread
# ---------------------------------------------------------------------------


def test_resolve_trace_id_prefers_env_over_active_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UIPATH_TRACE_ID wins over a live span — keeps native + compensation on one trace."""
    from opentelemetry.sdk.trace import TracerProvider

    monkeypatch.setenv("UIPATH_TRACE_ID", "env-trace-0001")
    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("root"):
        assert _resolve_trace_id("fallback-id") == "env-trace-0001"


def test_resolve_trace_id_falls_back_to_active_span_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With UIPATH_TRACE_ID unset, the live span's trace id is used."""
    from opentelemetry.sdk.trace import TracerProvider

    monkeypatch.delenv("UIPATH_TRACE_ID", raising=False)
    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("root") as span:
        expected = format(span.get_span_context().trace_id, "032x")
        result = _resolve_trace_id("fallback-id")
        assert result == expected
        assert len(result) == 32  # dashless OTel hex, not a dashed uuid


def test_resolve_trace_id_uses_fallback_without_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no active span and no UIPATH_TRACE_ID env, fallback wins."""
    monkeypatch.delenv("UIPATH_TRACE_ID", raising=False)
    assert _resolve_trace_id("fallback-id") == "fallback-id"


def test_submit_compensation_captures_live_trace_before_thread_hop() -> None:
    """End-to-end thread-boundary proof.

    ``submit_compensation`` runs on the caller (hook) thread, then hands the
    compensation call to a background worker pool. The trace id must be
    resolved on the caller (where the OTel span is live) and carried into
    the worker — the worker has no live OTel context.
    """
    from opentelemetry.sdk.trace import TracerProvider

    tracer = TracerProvider().get_tracer("test")
    provider = _provider()

    done = threading.Event()
    captured: dict[str, Any] = {}

    def _capture(request: GovernRequest) -> None:
        # Runs on the background worker thread.
        captured["trace_id"] = request.trace_id
        # Prove the worker has NO live context: resolving here falls back.
        captured["worker_resolves_to"] = _resolve_trace_id("WORKER-MISS")
        done.set()

    provider.compensate.side_effect = _capture

    with tracer.start_as_current_span("agent-run") as span:
        expected = format(span.get_span_context().trace_id, "032x")
        submit_compensation(
            provider,
            _rules("pii_detection"),
            {"content": "x"},
            "before_model",
            "stale-fallback",  # must be overridden by the live trace
            "2026-06-06T00:00:00Z",
            "agent",
            "rt",
        )
    assert done.wait(timeout=2.0), "compensation worker never ran"

    # (1) worker thread could not see the span — fell back to the sentinel
    assert captured["worker_resolves_to"] == "WORKER-MISS"
    # (2) the value the provider received is the live span trace, captured pre-hop
    assert captured["trace_id"] == expected
    assert captured["trace_id"] != "stale-fallback"


# ---------------------------------------------------------------------------
# Evaluator integration — skipped until evaluator.py lands on this branch
# ---------------------------------------------------------------------------


_skip_no_evaluator = pytest.mark.skipif(
    not _HAS_EVALUATOR,
    reason=(
        "evaluator module not present on this branch; "
        "tests must be rewritten when it lands to match the new "
        "provider-first submit_compensation signature"
    ),
)


def _guardrail_fallback_rule() -> Rule:
    """A rule whose only check is a guardrail_fallback condition."""
    return Rule(
        rule_id="UIP-GR-01",
        name="PII guardrail (UiPath-mapped, disabled)",
        clause="UiPath-Mapped Guardrail",
        hook=LifecycleHook.BEFORE_MODEL,
        action=Action.AUDIT,
        checks=[
            Check(
                conditions=[
                    Condition(
                        operator="guardrail_fallback",
                        field="",
                        value={
                            "validator": "pii_detection",
                            "mapped_to_uipath": True,
                            "policy_enabled": False,
                        },
                    )
                ],
                action=Action.AUDIT,
                message="PII guardrail disabled",
            )
        ],
    )


def _build_index_with(rule: Rule) -> PolicyIndex:
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


@_skip_no_evaluator
def test_evaluator_dispatches_compensation_for_fired_guardrail() -> None:
    """A matched guardrail_fallback rule must trigger the provider."""
    set_enforcement_mode(EnforcementMode.AUDIT)
    evaluator = GovernanceEvaluator(_build_index_with(_guardrail_fallback_rule()))

    ctx = CheckContext(
        hook=LifecycleHook.BEFORE_MODEL,
        agent_name="agent-x",
        runtime_id="run-1",
        trace_id="trace-1",
        model_input="contact jane@acme.com",
    )

    # NOTE: this test needs to be rewritten when the evaluator lands —
    # the new signature is ``submit_compensation(provider, rules, ...)``
    # and the evaluator must thread a provider through to the call site.
    audit = evaluator.evaluate(ctx)
    assert audit.final_action == Action.AUDIT
    assert audit.rules_matched == 1


@_skip_no_evaluator
def test_evaluator_does_not_emit_audit_trace_for_guardrail_fallback_rule() -> None:
    """Python must not emit a per-rule audit trace for guardrail_fallback.

    The governance-server writes the trace from its side; emitting one
    here would duplicate. The rule still appears in the AuditRecord so
    ``disabled_guardrails`` can find it.
    """
    from uipath.runtime.governance._audit.base import (
        AuditEvent,
        AuditSink,
        EventType,
        get_audit_manager,
        reset_audit_manager,
    )

    class _CapturingSink(AuditSink):
        def __init__(self) -> None:
            self.events: list[AuditEvent] = []

        @property
        def name(self) -> str:
            return "capturing"

        def emit(self, event: AuditEvent) -> None:
            self.events.append(event)

    reset_audit_manager()
    try:
        manager = get_audit_manager()
        for existing in list(manager.list_sinks()):
            manager.unregister_sink(existing)
        sink = _CapturingSink()
        manager.register_sink(sink)
        manager._async_mode = False  # synchronous emission for assertions

        set_enforcement_mode(EnforcementMode.AUDIT)
        evaluator = GovernanceEvaluator(
            _build_index_with(_guardrail_fallback_rule())
        )

        ctx = CheckContext(
            hook=LifecycleHook.BEFORE_MODEL,
            agent_name="agent-x",
            runtime_id="run-1",
            trace_id="trace-1",
            model_input="hi",
        )

        audit = evaluator.evaluate(ctx)
        time.sleep(0.05)

        assert audit.rules_matched == 1
        assert any(
            ev.matched and ev.rule_id == "UIP-GR-01" for ev in audit.evaluations
        )

        rule_events = [
            e for e in sink.events if e.event_type == EventType.RULE_EVALUATION
        ]
        assert not any(
            e.data.get("rule_id") == "UIP-GR-01" for e in rule_events
        ), "guardrail_fallback rule must not emit a Python-side audit trace"

        summaries = [
            e for e in sink.events if e.event_type == EventType.HOOK_END
        ]
        assert len(summaries) == 1
        assert summaries[0].data["total_rules"] == 0
        assert summaries[0].data["matched_rules"] == 0
    finally:
        reset_audit_manager()
