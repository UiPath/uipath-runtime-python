"""Tests for the default enforcement-mode resolution.

The default is :attr:`EnforcementMode.DISABLED` — until the policy
loader successfully fetches a backend response and calls
``set_enforcement_mode`` with the server-supplied value, governance
short-circuits cheaply with no per-call audit overhead.

Resolution order (per :func:`get_enforcement_mode`):
1. Previously-cached programmatic value (set via ``set_enforcement_mode``).
2. ``UIPATH_GOVERNANCE_MODE`` env var.
3. Default ``DISABLED``.
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


def test_default_mode_is_disabled() -> None:
    """No programmatic mode + no env var → DISABLED.

    Replaces the prior AUDIT default. Empty-policy / failed-fetch /
    pre-fetch tenants pay zero audit overhead until the backend
    explicitly enables governance on the next policy fetch.
    """
    assert get_enforcement_mode() is EnforcementMode.DISABLED


def test_env_var_audit_wins_over_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Developer override via env var still works."""
    monkeypatch.setenv("UIPATH_GOVERNANCE_MODE", "audit")
    reset_enforcement_mode()  # clear cached default
    assert get_enforcement_mode() is EnforcementMode.AUDIT


def test_env_var_enforce_wins_over_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UIPATH_GOVERNANCE_MODE", "enforce")
    reset_enforcement_mode()
    assert get_enforcement_mode() is EnforcementMode.ENFORCE


def test_invalid_env_var_falls_back_to_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UIPATH_GOVERNANCE_MODE", "garbage-value")
    reset_enforcement_mode()
    assert get_enforcement_mode() is EnforcementMode.DISABLED


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
    assert get_enforcement_mode() is EnforcementMode.DISABLED


def test_disabled_mode_is_cached_after_first_read() -> None:
    """First call computes; subsequent calls hit the cache."""
    assert get_enforcement_mode() is EnforcementMode.DISABLED
    # A second call returns the same instance — the cache survives.
    assert get_enforcement_mode() is EnforcementMode.DISABLED
