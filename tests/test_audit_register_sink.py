"""Tests for ``AuditManager.register_sink`` failure-counter semantics.

A re-registered same-name sink must NOT inherit the previous instance's
tripped circuit-breaker state. ``unregister_sink`` already clears these
counters, but ``register_sink`` also clears them on a successful add as
defense-in-depth (covers tests / external callers that touch the
internal counter dicts directly).
"""

from __future__ import annotations

from typing import Any

import pytest

from uipath.runtime.governance._audit.base import (
    AuditEvent,
    AuditManager,
    AuditSink,
    EventType,
)


class _NoopSink(AuditSink):
    """Sink that records emit calls and never raises."""

    def __init__(self, name: str = "test-sink") -> None:
        self._name = name
        self.events: list[AuditEvent] = []

    @property
    def name(self) -> str:
        return self._name

    def emit(self, event: AuditEvent) -> None:
        self.events.append(event)


def _event() -> AuditEvent:
    return AuditEvent(event_type=EventType.RULE_EVALUATION, agent_name="a")


@pytest.fixture
def manager() -> Any:
    """Build a fresh, sync-mode AuditManager with no default sinks.

    ``register_default_sinks=False`` keeps the traces sink (and the
    per-instance atexit hook) out of the test, so assertions about
    registered sinks see only what the test puts there.
    """
    return AuditManager(async_mode=False, register_default_sinks=False)


def test_register_clears_stale_failure_counter(manager: AuditManager) -> None:
    """A new sink with a name that previously tripped starts fresh."""
    # Simulate prior instance having tripped the circuit-breaker without
    # going through unregister (e.g. test code or external code that
    # mutated the counters directly).
    manager._sink_failures["test-sink"] = manager._SINK_FAILURE_THRESHOLD
    manager._tripped_sinks.add("test-sink")

    new_sink = _NoopSink(name="test-sink")
    manager.register_sink(new_sink)

    # Counter and tripped-set must be cleared.
    assert manager._sink_failures.get("test-sink", 0) == 0
    assert "test-sink" not in manager._tripped_sinks

    # And the new sink actually receives events (would be skipped if
    # still considered tripped).
    manager.emit(_event())
    assert len(new_sink.events) == 1


def test_register_does_not_clear_for_duplicate(manager: AuditManager) -> None:
    """Re-registering an already-present sink is a no-op (no counter reset)."""
    sink = _NoopSink(name="test-sink")
    manager.register_sink(sink)

    # Simulate the existing sink having accumulated some failures.
    manager._sink_failures["test-sink"] = 3

    # A second register call with the same name should NOT clear those
    # failures — the duplicate-check fires before the reset.
    duplicate = _NoopSink(name="test-sink")
    manager.register_sink(duplicate)

    assert manager._sink_failures["test-sink"] == 3


def test_unregister_then_register_starts_fresh(manager: AuditManager) -> None:
    """The full lifecycle: register → trip → unregister → register again."""
    sink = _NoopSink(name="test-sink")
    manager.register_sink(sink)
    manager._sink_failures["test-sink"] = manager._SINK_FAILURE_THRESHOLD
    manager._tripped_sinks.add("test-sink")

    manager.unregister_sink("test-sink")
    # Unregister already clears.
    assert "test-sink" not in manager._tripped_sinks

    new_sink = _NoopSink(name="test-sink")
    manager.register_sink(new_sink)
    assert manager._sink_failures.get("test-sink", 0) == 0
    assert "test-sink" not in manager._tripped_sinks

    manager.emit(_event())
    assert len(new_sink.events) == 1
