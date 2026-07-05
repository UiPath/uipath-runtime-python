"""Tests for :func:`uipath.runtime.governance._audit.factory.create_sink`.

The factory is the entry point :class:`AuditManager` uses to construct
its always-on ``traces`` sink at init. It's tiny (one name → one sink)
but the unknown-name path deserves a regression guard so a future
refactor can't silently drop a sink registration.
"""

from __future__ import annotations

import logging

import pytest

from uipath.runtime.governance._audit.factory import create_sink
from uipath.runtime.governance._audit.traces import TracesAuditSink


def test_create_sink_traces_returns_traces_audit_sink() -> None:
    """The single supported name resolves to a real ``TracesAuditSink``."""
    sink = create_sink("traces")
    assert isinstance(sink, TracesAuditSink)


def test_create_sink_is_name_case_insensitive() -> None:
    """Callers can pass any casing — the factory normalizes internally.

    Regression guard: dropping the ``.lower()`` would silently break
    every consumer that hand-typed a name with different casing.
    """
    assert isinstance(create_sink("TRACES"), TracesAuditSink)
    assert isinstance(create_sink("Traces"), TracesAuditSink)


def test_create_sink_unknown_name_returns_none_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unknown name returns ``None`` (no crash) and logs a warning.

    Contract: registration failures must not raise — the audit manager's
    ``_register_traces_sink`` handles ``None`` by skipping the sink,
    which is safer than crashing at construction on a bad config.
    """
    with caplog.at_level(
        logging.WARNING, logger="uipath.runtime.governance._audit.factory"
    ):
        result = create_sink("unknown-sink")

    assert result is None
    assert any("Unknown audit sink" in r.message for r in caplog.records)
