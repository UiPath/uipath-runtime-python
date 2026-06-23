"""Tests for the default enforcement-mode resolution.

The default is :attr:`EnforcementMode.AUDIT` so the wrapper attaches at
runtime construction and the background policy load can run. If the
provider later returns ``disabled``, ``set_enforcement_mode`` flips
the mode and ``evaluate()`` short-circuits per-call.

Resolution (per :func:`get_enforcement_mode`):
1. The provider-supplied value applied via ``set_enforcement_mode`` by
   the policy loader.
2. Default ``AUDIT``.
"""

from __future__ import annotations

import pytest

from tests._helpers import reset_enforcement_mode
from uipath.runtime.governance.config import (
    EnforcementMode,
    get_enforcement_mode,
    set_enforcement_mode,
)


@pytest.fixture(autouse=True)
def _isolate_mode():
    """Each test starts from a clean module-state slate."""
    reset_enforcement_mode()
    yield
    reset_enforcement_mode()


def test_default_mode_is_audit() -> None:
    """No backend-supplied mode → AUDIT.

    AUDIT is the default so the wrapper attaches and the background
    policy fetch can run. The backend can flip the mode to DISABLED
    on fetch when the tenant has no policies.
    """
    assert get_enforcement_mode() is EnforcementMode.AUDIT


def test_backend_disabled_wins_over_default() -> None:
    """The backend mode (via ``set_enforcement_mode``) overrides the default."""
    set_enforcement_mode(EnforcementMode.DISABLED)
    assert get_enforcement_mode() is EnforcementMode.DISABLED


def test_backend_enforce_wins_over_default() -> None:
    set_enforcement_mode(EnforcementMode.ENFORCE)
    assert get_enforcement_mode() is EnforcementMode.ENFORCE


def test_reset_returns_to_default() -> None:
    """``reset_enforcement_mode`` clears the mode so the default re-applies."""
    set_enforcement_mode(EnforcementMode.ENFORCE)
    assert get_enforcement_mode() is EnforcementMode.ENFORCE
    reset_enforcement_mode()
    assert get_enforcement_mode() is EnforcementMode.AUDIT