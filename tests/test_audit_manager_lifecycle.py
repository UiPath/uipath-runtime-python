"""Lifecycle tests for :class:`AuditManager`.

Pins the production-readiness invariants of the audit manager:

- Construction is side-effect-free: no background thread, no atexit
  registration, no global state mutation.
- :meth:`close` is idempotent.
- :meth:`emit` dispatches on the caller's thread, so an OTel-backed
  sink sees the caller's live span without any cross-thread plumbing.
"""

from __future__ import annotations

import threading
from typing import Any

from uipath.runtime.governance._audit.base import (
    AuditEvent,
    AuditManager,
    AuditSink,
    EventType,
)

# ---------------------------------------------------------------------------
# Construction is side-effect-free
# ---------------------------------------------------------------------------


def test_construction_starts_no_background_thread() -> None:
    """``AuditManager()`` must not spawn a worker thread.

    Regression guard for the design pivot: the audit pipeline used to
    construct a daemon worker thread eagerly. Construction now only
    builds in-memory state; any async export lives inside the sink.
    """
    before = {t.name for t in threading.enumerate()}
    m = AuditManager(register_default_sinks=False)
    after = {t.name for t in threading.enumerate()}
    try:
        assert after == before, (
            f"AuditManager() spawned a thread; new threads: {after - before}"
        )
    finally:
        m.close()


def test_default_sink_registered_on_construction() -> None:
    """With defaults, the traces sink is auto-registered."""
    m = AuditManager()
    try:
        assert "traces" in m.list_sinks()
    finally:
        m.close()


def test_bare_construction_skips_default_sink() -> None:
    """``register_default_sinks=False`` produces an empty manager."""
    m = AuditManager(register_default_sinks=False)
    try:
        assert m.list_sinks() == []
    finally:
        m.close()


# ---------------------------------------------------------------------------
# close() is idempotent and clears sinks
# ---------------------------------------------------------------------------


def test_close_clears_sinks_and_failure_state() -> None:
    """``close()`` empties sinks, failure counters, and tripped set."""

    class _Sink(AuditSink):
        def __init__(self, name: str) -> None:
            self._name = name
            self.closed = False

        @property
        def name(self) -> str:
            return self._name

        def emit(self, event: AuditEvent) -> None:
            pass

        def close(self) -> None:
            self.closed = True

    m = AuditManager(register_default_sinks=False)
    s = _Sink("test")
    m.register_sink(s)
    m._sink_failures["test"] = 3
    m._tripped_sinks.add("test")

    m.close()

    assert m.list_sinks() == []
    assert m._sink_failures == {}
    assert m._tripped_sinks == set()
    assert s.closed


def test_close_is_idempotent() -> None:
    """Calling ``close()`` twice must not raise."""
    m = AuditManager(register_default_sinks=False)
    m.close()
    m.close()  # must not raise


# ---------------------------------------------------------------------------
# flush() delegates to every sink
# ---------------------------------------------------------------------------


def test_flush_calls_flush_on_each_sink() -> None:
    """The manager holds no buffer; ``flush()`` is a fan-out to sinks."""

    class _Sink(AuditSink):
        def __init__(self, name: str) -> None:
            self._name = name
            self.flush_count = 0

        @property
        def name(self) -> str:
            return self._name

        def emit(self, event: AuditEvent) -> None:
            pass

        def flush(self) -> None:
            self.flush_count += 1

    m = AuditManager(register_default_sinks=False)
    a, b = _Sink("a"), _Sink("b")
    m.register_sink(a)
    m.register_sink(b)
    try:
        m.flush()
        assert a.flush_count == 1
        assert b.flush_count == 1
    finally:
        m.close()


# ---------------------------------------------------------------------------
# emit() runs on the caller's thread — OTel context visible directly
# ---------------------------------------------------------------------------


def test_emit_runs_on_caller_thread() -> None:
    """``emit()`` invokes sinks synchronously on the calling thread.

    Asserts the design contract that lets OTel-backed sinks see the
    agent's live span via ``trace.get_current_span()`` without any
    cross-thread context propagation.
    """
    captured: dict[str, Any] = {}

    class _Probe(AuditSink):
        @property
        def name(self) -> str:
            return "probe"

        def emit(self, event: AuditEvent) -> None:
            captured["thread"] = threading.current_thread()

    m = AuditManager(register_default_sinks=False)
    m.register_sink(_Probe())
    try:
        m.emit(AuditEvent(event_type=EventType.RULE_EVALUATION))
        assert captured["thread"] is threading.current_thread()
    finally:
        m.close()


def test_emit_propagates_otel_span_via_current_context() -> None:
    """An OTel-backed sink sees the caller's live span directly.

    With sync dispatch there's no contextvars snapshot/restore — the
    sink just calls ``trace.get_current_span()`` on the same thread the
    caller is on, and that's the span the caller has active.
    """
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    captured: dict[str, Any] = {}

    class _Probe(AuditSink):
        @property
        def name(self) -> str:
            return "probe"

        def emit(self, event: AuditEvent) -> None:
            sc = trace.get_current_span().get_span_context()
            captured["trace_id"] = sc.trace_id if sc.is_valid else None
            captured["span_id"] = sc.span_id if sc.is_valid else None

    tracer = TracerProvider().get_tracer("test")
    m = AuditManager(register_default_sinks=False)
    m.register_sink(_Probe())
    try:
        with tracer.start_as_current_span("agent-run") as span:
            expected = span.get_span_context()
            m.emit(AuditEvent(event_type=EventType.RULE_EVALUATION))
        assert captured["trace_id"] == expected.trace_id
        assert captured["span_id"] == expected.span_id
    finally:
        m.close()
