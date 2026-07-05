"""Tests for :class:`GovernanceRuntimeMetadata`.

The dataclass carries the per-runtime constants every governance
telemetry event stamps. Defaults must keep telemetry flowing when the
host hasn't populated agent type / framework yet, and the runtime
version must resolve from installed package metadata when available.
"""

from __future__ import annotations

from unittest.mock import patch

from uipath.runtime.governance._audit.metadata import (
    NATIVE_EXECUTION_ENGINE,
    GovernanceRuntimeMetadata,
    _resolve_runtime_version,
)


def test_defaults() -> None:
    """Default-constructed metadata uses native engine + ``unknown`` slots."""
    meta = GovernanceRuntimeMetadata()
    assert meta.execution_engine == NATIVE_EXECUTION_ENGINE
    assert meta.agent_type == "unknown"
    assert meta.agent_framework == "unknown"
    # runtime_version is resolved at construction; either real or "unknown"
    assert isinstance(meta.runtime_version, str)
    assert meta.runtime_version != ""


def test_explicit_overrides_persist() -> None:
    """Host-supplied values override the defaults verbatim."""
    meta = GovernanceRuntimeMetadata(
        execution_engine="agt",
        agent_type="uipath_coded",
        agent_framework="langchain",
        runtime_version="1.2.3",
    )
    assert meta.execution_engine == "agt"
    assert meta.agent_type == "uipath_coded"
    assert meta.agent_framework == "langchain"
    assert meta.runtime_version == "1.2.3"


def test_frozen() -> None:
    """Dataclass is frozen — host can't mutate per-run constants mid-run."""
    meta = GovernanceRuntimeMetadata()
    try:
        meta.agent_type = "other"  # type: ignore[misc]
    except Exception as exc:
        assert "frozen" in str(exc).lower() or "cannot assign" in str(exc).lower()
    else:
        raise AssertionError("frozen dataclass must reject attribute writes")


def test_as_payload_contains_all_four_fields() -> None:
    """``as_payload`` is the canonical merge-into-event-data dict shape."""
    meta = GovernanceRuntimeMetadata(
        execution_engine="agt",
        agent_type="uipath_coded",
        agent_framework="langchain",
        runtime_version="1.2.3",
    )
    payload = meta.as_payload()
    assert payload == {
        "execution_engine": "agt",
        "agent_type": "uipath_coded",
        "agent_framework": "langchain",
        "runtime_version": "1.2.3",
    }


def test_runtime_version_fallback_on_missing_package() -> None:
    """A source checkout with no installed metadata falls back to ``unknown``."""
    from importlib.metadata import PackageNotFoundError

    with patch(
        "uipath.runtime.governance._audit.metadata.version",
        side_effect=PackageNotFoundError("uipath-runtime"),
    ):
        assert _resolve_runtime_version() == "unknown"
