"""Tests for the instance-scoped GuardrailCompensator.

The runtime layer owns only the bounded background pool and the
trace-id capture; HTTP/auth/URL/header concerns live behind the
:class:`uipath.core.governance.GovernanceCompensationProvider` protocol
and are exercised in ``uipath-platform``'s own tests.

These tests cover:

- ``disabled_guardrails`` — distilling fired ``guardrail_fallback`` rules
  into per-rule wire metadata.
- ``GuardrailCompensator.submit`` — pool routing, in-flight
  backpressure, shutdown safety, wire-model assembly, and the
  thread-boundary trace-id capture.
- ``_resolve_trace_id`` — env > live OTel span > fallback ordering.
- Cross-instance isolation — two compensators do not share a pool or
  semaphore.
- Process-level cleanup — one ``atexit`` registration, weak refs only.
"""

from __future__ import annotations

import gc
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from uipath.core.governance import (
    FiredRule,
    GovernanceCompensationProvider,
    GovernRequest,
)

from uipath.runtime.governance.native import guardrail_compensation
from uipath.runtime.governance.native.guardrail_compensation import (
    GuardrailCompensator,
    _resolve_trace_id,
    disabled_guardrails,
)

# Evaluator integration is not present on this branch — the evaluator
# module (which would consume the compensator) lands in a later slice.
# Tests that exercise the full dispatch path skip until then.
_HAS_EVALUATOR = False
try:
    from uipath.runtime.governance.native.evaluator import (  # type: ignore[import-not-found]  # noqa: F401
        GovernanceEvaluator,
    )

    _HAS_EVALUATOR = True
except ImportError:
    pass


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


def _run_inline(compensator: GuardrailCompensator) -> None:
    """Replace the pool's ``submit`` with synchronous execution.

    Lets tests assert provider behavior deterministically without
    relying on wait()/sleep().
    """

    def _sync_submit(fn: Any, *args: Any, **kwargs: Any) -> None:
        fn()

    compensator._pool.submit = _sync_submit  # type: ignore[method-assign]


@pytest.fixture(autouse=True)
def _close_dangling_compensators() -> Any:
    """Best-effort teardown: close any compensator weak-refs still in the set.

    Each test should call ``compensator.close()``, but a failing
    assertion mid-test could leak. The sweep prevents pytest from
    hanging at exit on a leftover worker pool.
    """
    yield
    for compensator in list(guardrail_compensation._live_compensators):
        try:
            compensator.close()
        except Exception:  # noqa: BLE001 - best-effort teardown
            pass
    guardrail_compensation._live_compensators.clear()


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
    """Mapped to UiPath AND enabled → no compensation needed."""
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
    """Not mapped to UiPath → server can't fall back; skip."""
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
# GuardrailCompensator.submit — short-circuits + pool routing + backpressure
# ---------------------------------------------------------------------------


def test_submit_empty_rules_short_circuits() -> None:
    """No rules → no pool submit, no provider call."""
    provider = _provider()
    compensator = GuardrailCompensator(provider)
    with patch.object(compensator, "_pool") as mock_pool:
        compensator.submit([], {}, "before_model", "t", "ts", "a", "r")
    mock_pool.submit.assert_not_called()
    provider.compensate.assert_not_called()


def test_submit_no_validators_short_circuits() -> None:
    """Rules with empty validator strings → no call (nothing to dispatch)."""
    provider = _provider()
    compensator = GuardrailCompensator(provider)
    rules = [FiredRule(rule_id="R", rule_name="n", pack_name="p", validator="")]
    with patch.object(compensator, "_pool") as mock_pool:
        compensator.submit(rules, {}, "before_model", "t", "ts", "a", "r")
    mock_pool.submit.assert_not_called()
    provider.compensate.assert_not_called()


def test_submit_routes_through_pool() -> None:
    """A non-empty rules list submits a single task to the pool."""
    provider = _provider()
    compensator = GuardrailCompensator(provider)
    with patch.object(compensator, "_pool") as mock_pool:
        compensator.submit(
            _rules("pii_detection"),
            {"content": "x"},
            "before_model",
            "trace-1",
            "ts",
            "agent",
            "run",
        )
    mock_pool.submit.assert_called_once()


def test_submit_drops_when_pool_saturated() -> None:
    """When the in-flight semaphore is exhausted, the call is dropped."""
    provider = _provider()
    compensator = GuardrailCompensator(provider)

    # Force the semaphore into "exhausted" state.
    drained = threading.BoundedSemaphore(1)
    drained.acquire()  # next acquire(blocking=False) returns False
    compensator._inflight = drained

    with patch.object(compensator, "_pool") as mock_pool:
        compensator.submit(
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


def test_submit_swallows_pool_shutdown_runtimeerror() -> None:
    """If the pool was shut down, submit must not raise."""

    class _ShutdownPool:
        def submit(self, fn: Any, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("cannot schedule new futures after shutdown")

    compensator = GuardrailCompensator(_provider())
    compensator._pool = _ShutdownPool()  # type: ignore[assignment]
    compensator._inflight = threading.BoundedSemaphore(4)

    # Must not raise.
    compensator.submit(_rules("x"), {}, "before_model", "t", "ts", "a", "r")


# ---------------------------------------------------------------------------
# GuardrailCompensator.submit — wire-model assembly + provider invocation
# ---------------------------------------------------------------------------


def test_submit_invokes_provider_with_govern_request() -> None:
    """The provider receives a GovernRequest carrying every wire field."""
    provider = _provider()
    compensator = GuardrailCompensator(provider)
    _run_inline(compensator)
    rules = _rules("pii_detection", "harmful_content")

    compensator.submit(
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


def test_submit_dedupes_validators() -> None:
    """Multiple rules with the same validator collapse on the wire."""
    provider = _provider()
    compensator = GuardrailCompensator(provider)
    _run_inline(compensator)
    rules = _rules("pii_detection") + _rules("pii_detection", rule_id="R2")

    compensator.submit(rules, {}, "before_model", "t", "ts", "a", "r")

    (request,) = provider.compensate.call_args.args
    assert request.validators == ["pii_detection"]
    # Per-rule metadata is preserved (one record per rule even with shared validator).
    assert len(request.rules) == 2


def test_submit_swallows_provider_errors() -> None:
    """A provider exception must never propagate to the caller / agent."""
    provider = _provider()
    provider.compensate.side_effect = RuntimeError("network down")
    compensator = GuardrailCompensator(provider)
    _run_inline(compensator)

    # Must not raise.
    compensator.submit(_rules("x"), {}, "before_model", "t", "ts", "a", "r")

    provider.compensate.assert_called_once()


def test_submit_releases_semaphore_on_provider_error() -> None:
    """Provider failure must not leak a semaphore slot."""
    provider = _provider()
    provider.compensate.side_effect = RuntimeError("transient")
    # 4 workers × 1 oversubscription = 4 slots total.
    compensator = GuardrailCompensator(provider, inflight_oversubscription=1)
    _run_inline(compensator)

    # Fire 8 — all 8 must reach the provider; the semaphore must release
    # on each error so the next submit can acquire.
    for _ in range(8):
        compensator.submit(_rules("x"), {}, "before_model", "t", "ts", "a", "r")

    assert provider.compensate.call_count == 8, (
        "All 8 submissions should fire — semaphore must release on error"
    )


# ---------------------------------------------------------------------------
# _resolve_trace_id — must capture the live trace on the caller thread
# ---------------------------------------------------------------------------


def test_resolve_trace_id_prefers_supplied_over_active_span() -> None:
    """Constructor-supplied trace id wins over a live span.

    The wiring layer (uipath CLI) reads ``UIPATH_TRACE_ID`` and passes
    the value into :class:`GuardrailCompensator`. That id is
    authoritative because native governance audit spans are exported
    under it (platform rebinds spans to the agent's run trace) and
    server-written compensation records must land on the same id.
    """
    from opentelemetry.sdk.trace import TracerProvider

    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("root"):
        assert _resolve_trace_id("supplied-0001", "fallback-id") == "supplied-0001"


def test_resolve_trace_id_falls_back_to_active_span_when_not_supplied() -> None:
    """No supplied id → the live span's trace id is used."""
    from opentelemetry.sdk.trace import TracerProvider

    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("root") as span:
        expected = format(span.get_span_context().trace_id, "032x")
        result = _resolve_trace_id(None, "fallback-id")
        assert result == expected
        assert len(result) == 32  # dashless OTel hex, not a dashed uuid


def test_resolve_trace_id_uses_fallback_without_context() -> None:
    """No supplied id and no active span → fallback wins."""
    assert _resolve_trace_id(None, "fallback-id") == "fallback-id"


def test_resolve_trace_id_does_not_read_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Runtime layer must not read host env vars; only the wiring layer does.

    Pin radu's PR #121 boundary rule for this code path. Even when
    ``UIPATH_TRACE_ID`` is set in the environment, ``_resolve_trace_id``
    ignores it — the wiring layer is solely responsible for env reads.
    """
    monkeypatch.setenv("UIPATH_TRACE_ID", "env-should-be-ignored")
    # No supplied, no active span → fallback should win, NOT the env value.
    assert _resolve_trace_id(None, "fallback-id") == "fallback-id"


def test_compensator_trace_id_overrides_caller_supplied_value() -> None:
    """A compensator constructed with ``trace_id`` stamps it on every dispatch.

    The wiring layer passes ``UIPATH_TRACE_ID`` into the compensator at
    construction; per-call ``trace_id`` arguments become only a fallback
    for the case where the constructor value is absent.
    """
    provider = _provider()
    compensator = GuardrailCompensator(provider, trace_id="wired-trace-0001")
    _run_inline(compensator)

    compensator.submit(
        _rules("pii_detection"),
        {},
        "before_model",
        "per-call-fallback",  # must lose to the constructor value
        "ts",
        "agent",
        "run",
    )

    (request,) = provider.compensate.call_args.args
    assert request.trace_id == "wired-trace-0001"


def test_submit_captures_live_trace_before_thread_hop() -> None:
    """End-to-end thread-boundary proof.

    ``submit`` runs on the caller (hook) thread, then hands the
    compensation call to a background worker pool. The trace id must
    be resolved on the caller (where the OTel span is live) and
    carried into the worker — the worker has no live OTel context.
    """
    from opentelemetry.sdk.trace import TracerProvider

    tracer = TracerProvider().get_tracer("test")
    provider = _provider()
    compensator = GuardrailCompensator(provider)

    done = threading.Event()
    captured: dict[str, Any] = {}

    def _capture(request: GovernRequest) -> None:
        # Runs on the background worker thread.
        captured["trace_id"] = request.trace_id
        # Prove the worker has NO live context: resolving here with no
        # supplied id and no live span falls all the way through to the
        # WORKER-MISS sentinel.
        captured["worker_resolves_to"] = _resolve_trace_id(None, "WORKER-MISS")
        done.set()

    provider.compensate.side_effect = _capture

    with tracer.start_as_current_span("agent-run") as span:
        expected = format(span.get_span_context().trace_id, "032x")
        compensator.submit(
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
# Cross-instance isolation — the architectural motivation for the refactor
# ---------------------------------------------------------------------------


def test_two_compensators_do_not_share_pool_or_semaphore() -> None:
    """Parallel runtimes cannot saturate each other's compensation pool."""
    p1 = _provider()
    p2 = _provider()
    c1 = GuardrailCompensator(p1)
    c2 = GuardrailCompensator(p2)

    assert c1._pool is not c2._pool
    assert c1._inflight is not c2._inflight

    # Drain c1's semaphore to its cap; c2 must remain unaffected.
    drained = threading.BoundedSemaphore(1)
    drained.acquire()
    c1._inflight = drained

    _run_inline(c2)
    c2.submit(_rules("pii_detection"), {}, "before_model", "t", "ts", "a", "r")
    p2.compensate.assert_called_once()
    p1.compensate.assert_not_called()


# ---------------------------------------------------------------------------
# Lifecycle — bounded atexit + weakref tracking (mirrors AuditManager pattern)
# ---------------------------------------------------------------------------


def test_three_compensators_register_one_process_atexit_hook() -> None:
    """N compensators → 1 atexit registration, not N.

    Regression: a per-instance ``atexit.register(self.close)`` would
    grow the atexit list linearly. The fix routes everyone through one
    process-level cleanup hook keyed by a WeakSet.
    """
    with patch.object(guardrail_compensation.atexit, "register") as mock_register:
        guardrail_compensation._atexit_registered = False
        GuardrailCompensator(_provider())
        GuardrailCompensator(_provider())
        GuardrailCompensator(_provider())
        assert mock_register.call_count == 1, (
            "Each compensator must NOT register its own atexit handler"
        )


def test_disposed_compensator_can_be_garbage_collected() -> None:
    """The WeakSet must NOT keep a disposed compensator alive."""
    import weakref

    compensator = GuardrailCompensator(_provider())
    ref = weakref.ref(compensator)

    assert compensator in guardrail_compensation._live_compensators

    compensator.close()
    del compensator
    gc.collect()

    assert ref() is None, (
        "GuardrailCompensator kept alive — strong reference leak in cleanup machinery"
    )


def test_process_cleanup_handles_already_closed_compensator() -> None:
    """If a compensator was explicitly closed, the process hook is a no-op for it."""
    c = GuardrailCompensator(_provider())
    c.close()
    # Must not raise.
    guardrail_compensation._process_cleanup_compensators()


def test_close_is_idempotent() -> None:
    """Calling close() twice is a logged no-op, not a crash."""
    c = GuardrailCompensator(_provider())
    c.close()
    c.close()  # must not raise
