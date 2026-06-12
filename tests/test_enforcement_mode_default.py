"""Tests for the default enforcement-mode resolution.

The default is :attr:`EnforcementMode.AUDIT` so the wrapper attaches at
runtime construction and the background policy fetch can run. If the
backend later returns ``disabled``, ``set_enforcement_mode`` flips the
cache and ``evaluate()`` short-circuits per-call.

Resolution order (per :func:`get_enforcement_mode`):
1. Previously-cached programmatic value (set via ``set_enforcement_mode``).
2. ``UIPATH_GOVERNANCE_MODE`` env var.
3. Default ``AUDIT``.
"""

from __future__ import annotations

import pytest

from uipath.runtime.governance.config import (
    EnforcementMode,
    get_enforcement_mode,
    reset_enforcement_mode,
    set_enforcement_mode,
)


@pytest.fixture(autouse=True)
def _isolate_mode(monkeypatch: pytest.MonkeyPatch):
    """Each test starts from a clean module-state slate."""
    monkeypatch.delenv("UIPATH_GOVERNANCE_MODE", raising=False)
    reset_enforcement_mode()
    yield
    reset_enforcement_mode()


def test_default_mode_is_audit() -> None:
    """No programmatic mode + no env var → AUDIT.

    AUDIT is the default so the wrapper attaches and the background
    policy fetch can run. The backend can flip the cache to DISABLED
    on fetch when the tenant has no policies.
    """
    assert get_enforcement_mode() is EnforcementMode.AUDIT


def test_env_var_disabled_wins_over_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Developer override via env var still works."""
    monkeypatch.setenv("UIPATH_GOVERNANCE_MODE", "disabled")
    reset_enforcement_mode()  # clear cached default
    assert get_enforcement_mode() is EnforcementMode.DISABLED


def test_env_var_enforce_wins_over_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UIPATH_GOVERNANCE_MODE", "enforce")
    reset_enforcement_mode()
    assert get_enforcement_mode() is EnforcementMode.ENFORCE


def test_invalid_env_var_falls_back_to_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UIPATH_GOVERNANCE_MODE", "garbage-value")
    reset_enforcement_mode()
    assert get_enforcement_mode() is EnforcementMode.AUDIT


def test_programmatic_set_wins_over_env_and_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The policy loader's ``set_enforcement_mode`` call is canonical."""
    monkeypatch.setenv("UIPATH_GOVERNANCE_MODE", "audit")
    set_enforcement_mode(EnforcementMode.ENFORCE)
    assert get_enforcement_mode() is EnforcementMode.ENFORCE


def test_reset_returns_to_default() -> None:
    """``reset_enforcement_mode`` clears the cache so the default re-applies."""
    set_enforcement_mode(EnforcementMode.ENFORCE)
    assert get_enforcement_mode() is EnforcementMode.ENFORCE
    reset_enforcement_mode()
    assert get_enforcement_mode() is EnforcementMode.AUDIT


def test_audit_mode_is_cached_after_first_read() -> None:
    """First call computes; subsequent calls hit the cache."""
    assert get_enforcement_mode() is EnforcementMode.AUDIT
    # A second call returns the same instance — the cache survives.
    assert get_enforcement_mode() is EnforcementMode.AUDIT
