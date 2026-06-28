"""Tests for the instance-scoped GuardrailCompensator.

The runtime layer owns only the bounded background pool and the
contextvars propagation that keeps live OTel context visible on the
worker thread. HTTP/auth/URL/header concerns — including ``trace_id``
resolution — live behind the
:class:`uipath.core.governance.GovernanceCompensationProvider` protocol
and are exercised in the concrete provider's own tests.

These tests cover:

- ``disabled_guardrails`` — distilling fired ``guardrail_fallback`` rules
  into per-rule wire metadata.
- ``GuardrailCompensator.submit`` — pool routing, in-flight
  backpressure, shutdown safety, wire-model assembly, and the
  ``contextvars.copy_context()`` propagation that keeps the agent's
  OTel span visible inside the worker callable.
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
    disabled_guardrails,
)

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
        # The compensator submits ``ctx.run, _run`` (the bound method
        # of a captured context plus the callable). Mirror that here so
        # the captured context still wraps the worker callable.
        if args:
            fn(*args, **kwargs)
        else:
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
        compensator.submit([], {}, "before_model", "ts", "a", "r")
    mock_pool.submit.assert_not_called()
    provider.compensate.assert_not_called()


def test_submit_no_validators_short_circuits() -> None:
    """Rules with empty validator strings → no call (nothing to dispatch)."""
    provider = _provider()
    compensator = GuardrailCompensator(provider)
    rules = [FiredRule(rule_id="R", rule_name="n", pack_name="p", validator="")]
    with patch.object(compensator, "_pool") as mock_pool:
        compensator.submit(rules, {}, "before_model", "ts", "a", "r")
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
    compensator.submit(_rules("x"), {}, "before_model", "ts", "a", "r")


# ---------------------------------------------------------------------------
# GuardrailCompensator.submit — wire-model assembly + provider invocation
# ---------------------------------------------------------------------------


def test_submit_invokes_provider_with_govern_request() -> None:
    """The provider receives a GovernRequest carrying every wire field.

    ``trace_id`` is left empty on the wire — the injected provider
    resolves it at HTTP-call time.
    """
    provider = _provider()
    compensator = GuardrailCompensator(provider)
    _run_inline(compensator)
    rules = _rules("pii_detection", "harmful_content")

    compensator.submit(
        rules,
        {"content": "x"},
        "before_model",
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
    # ``trace_id`` is intentionally empty — the provider resolves at HTTP time.
    assert request.trace_id == ""
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

    compensator.submit(rules, {}, "before_model", "ts", "a", "r")

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
    compensator.submit(_rules("x"), {}, "before_model", "ts", "a", "r")

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
        compensator.submit(_rules("x"), {}, "before_model", "ts", "a", "r")

    assert provider.compensate.call_count == 8, (
        "All 8 submissions should fire — semaphore must release on error"
    )


# ---------------------------------------------------------------------------
# contextvars propagation — live OTel context visible inside the worker
# ---------------------------------------------------------------------------


def test_submit_propagates_otel_context_to_worker_thread() -> None:
    """The worker callable runs inside the caller's contextvars snapshot.

    Without ``contextvars.copy_context()``, a worker thread started by
    ``ThreadPoolExecutor`` would see an empty OTel context — the
    the provider could only resolve env-based trace ids on the worker.
    With the snapshot, the worker sees the same live span the agent
    hook saw, so the provider can resolve the agent's actual trace id.
    """
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    tracer = TracerProvider().get_tracer("test")
    provider = _provider()
    compensator = GuardrailCompensator(provider)

    done = threading.Event()
    captured: dict[str, Any] = {}

    def _capture(request: GovernRequest) -> None:
        # Runs on the worker thread but inside the captured context —
        # the agent's live span should still be visible here.
        ctx = trace.get_current_span().get_span_context()
        captured["worker_trace_id_hex"] = (
            format(ctx.trace_id, "032x") if ctx.is_valid else ""
        )
        captured["worker_thread_name"] = threading.current_thread().name
        done.set()

    provider.compensate.side_effect = _capture

    with tracer.start_as_current_span("agent-run") as span:
        expected = format(span.get_span_context().trace_id, "032x")
        compensator.submit(
            _rules("pii_detection"),
            {"content": "x"},
            "before_model",
            "2026-06-06T00:00:00Z",
            "agent",
            "rt",
        )
    assert done.wait(timeout=2.0), "compensation worker never ran"

    # Worker ran on the dedicated pool thread (not the caller).
    assert captured["worker_thread_name"].startswith("governance-compensation")
    # And the captured contextvars context propagated the OTel span across
    # the thread hop — the worker sees the same trace_id the agent saw.
    assert captured["worker_trace_id_hex"] == expected


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
    c2.submit(_rules("pii_detection"), {}, "before_model", "ts", "a", "r")
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
