"""Tests for ``AuditManager`` auto-registration of the ``track_events`` sink.

The sink is platform-mandated, like ``traces``. The host wires the
``track_event`` callable + ``GovernanceRuntimeMetadata`` at
construction. ``_register_track_event_sink`` mirrors the simple shape
of ``_register_traces_sink`` — deferred import, construct, register,
log — wrapped in a broad ``except`` so a misconfigured wiring layer
(missing callable, sink-construction error) is surfaced as a warning
instead of crashing the agent.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from uipath.runtime.governance._audit.base import AuditManager
from uipath.runtime.governance._audit.metadata import GovernanceRuntimeMetadata
from uipath.runtime.governance._audit.track_events import TrackEventAuditSink


class _Capture:
    """Stand-in for ``provider.track_event`` — records calls."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


# ---------------------------------------------------------------------------
# Happy path — host wires the callable
# ---------------------------------------------------------------------------


def test_track_event_sink_registered_when_callable_supplied() -> None:
    mgr = AuditManager(
        track_event=_Capture(),
        runtime_metadata=GovernanceRuntimeMetadata(agent_type="uipath_coded"),
    )
    try:
        sinks = mgr.list_sinks()
        assert TrackEventAuditSink.SINK_NAME in sinks
        sink = mgr.get_sink(TrackEventAuditSink.SINK_NAME)
        assert isinstance(sink, TrackEventAuditSink)
    finally:
        mgr.close()


def test_traces_sink_is_still_registered_alongside() -> None:
    """track_events doesn't replace traces — both are mandatory."""
    mgr = AuditManager(
        track_event=_Capture(),
        runtime_metadata=GovernanceRuntimeMetadata(),
    )
    try:
        sinks = mgr.list_sinks()
        assert "traces" in sinks
        assert TrackEventAuditSink.SINK_NAME in sinks
    finally:
        mgr.close()


def test_supplied_runtime_metadata_reaches_sink() -> None:
    mgr = AuditManager(
        track_event=_Capture(),
        runtime_metadata=GovernanceRuntimeMetadata(
            agent_type="uipath_coded", agent_framework="langchain"
        ),
    )
    try:
        sink = mgr.get_sink(TrackEventAuditSink.SINK_NAME)
        assert isinstance(sink, TrackEventAuditSink)
        # Internal: confirm the sink carries the host-supplied metadata.
        assert sink._meta.agent_type == "uipath_coded"
        assert sink._meta.agent_framework == "langchain"
    finally:
        mgr.close()


def test_metadata_defaults_when_not_supplied() -> None:
    """Helper falls back to ``GovernanceRuntimeMetadata()`` defaults."""
    mgr = AuditManager(track_event=_Capture())
    try:
        sink = mgr.get_sink(TrackEventAuditSink.SINK_NAME)
        assert isinstance(sink, TrackEventAuditSink)
        assert sink._meta.agent_type == "unknown"
        assert sink._meta.agent_framework == "unknown"
    finally:
        mgr.close()


# ---------------------------------------------------------------------------
# Missing-callable path — caught by the helper's try/except, logged
# ---------------------------------------------------------------------------


def test_missing_callable_logs_warning_and_skips_sink(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``track_event=None`` → sink __init__ raises → helper catches + warns."""
    caplog.set_level(logging.WARNING, logger="uipath.runtime.governance._audit.base")
    mgr = AuditManager()  # no track_event supplied
    try:
        # Traces sink still wired; track_events skipped because sink
        # construction raised ValueError caught by the helper.
        assert "traces" in mgr.list_sinks()
        assert TrackEventAuditSink.SINK_NAME not in mgr.list_sinks()

        warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("Failed to register track_events sink" in m for m in warnings), (
            f"expected registration-failure warning, got: {warnings}"
        )
    finally:
        mgr.close()


def test_sink_construction_error_does_not_crash_manager(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exception inside ``TrackEventAuditSink.__init__`` is swallowed.

    Forces a non-``ValueError`` exception (a raise inside ``__init__``
    triggered via the runtime_metadata path) to confirm the helper's
    ``except Exception`` covers the broad case the user asked for, not
    just the validation error.
    """
    caplog.set_level(logging.WARNING, logger="uipath.runtime.governance._audit.base")

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("synthetic registration failure")

    from uipath.runtime.governance._audit import track_events as te

    monkeypatch.setattr(te, "TrackEventAuditSink", _boom)

    mgr = AuditManager(track_event=_Capture())
    try:
        # Helper swallowed the RuntimeError; manager kept the traces sink
        # and reached a constructed state without crashing.
        assert "traces" in mgr.list_sinks()
        assert TrackEventAuditSink.SINK_NAME not in mgr.list_sinks()
        warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any(
            "Failed to register track_events sink" in m
            and "synthetic registration failure" in m
            for m in warnings
        ), f"expected wrapped registration failure, got: {warnings}"
    finally:
        mgr.close()


# ---------------------------------------------------------------------------
# Opt-out path — register_default_sinks=False
# ---------------------------------------------------------------------------


def test_opt_out_skips_both_sinks() -> None:
    """register_default_sinks=False keeps the audit pipeline empty for tests."""
    mgr = AuditManager(register_default_sinks=False)
    try:
        assert mgr.list_sinks() == []
    finally:
        mgr.close()


def test_opt_out_does_not_warn(caplog: pytest.LogCaptureFixture) -> None:
    """Opting out is an explicit signal — no missing-callable warning."""
    caplog.set_level(logging.WARNING, logger="uipath.runtime.governance._audit.base")
    mgr = AuditManager(register_default_sinks=False)
    try:
        warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert not any("track_events" in m for m in warnings)
    finally:
        mgr.close()
