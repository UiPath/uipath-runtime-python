"""Runtime-level governance enforcement-mode state.

``EnforcementMode`` is defined in :mod:`uipath.core.governance` and
re-exported here for backward compatibility. The get/set/reset helpers
below are *per-process* state — the native policy loader sets the mode
on each successful backend fetch.
"""

from __future__ import annotations

import logging
import os

from uipath.core.governance import EnforcementMode

logger = logging.getLogger(__name__)

ENV_ENFORCEMENT_MODE = "UIPATH_GOVERNANCE_MODE"

__all__ = [
    "EnforcementMode",
    "ENV_ENFORCEMENT_MODE",
    "get_enforcement_mode",
    "set_enforcement_mode",
    "reset_enforcement_mode",
]

_enforcement_mode: EnforcementMode | None = None


def get_enforcement_mode() -> EnforcementMode:
    """Return the current enforcement mode.

    Resolution order:

    1. A value previously set via :func:`set_enforcement_mode` (the
       policy loader calls this with the backend-supplied mode on every
       successful fetch — that's the canonical source).
    2. ``UIPATH_GOVERNANCE_MODE`` env var (developer override).
    3. Default :attr:`EnforcementMode.AUDIT` — evaluate and log without
       blocking.
    """
    global _enforcement_mode
    if _enforcement_mode is not None:
        return _enforcement_mode

    mode_str = os.getenv(ENV_ENFORCEMENT_MODE, "audit").lower()
    try:
        _enforcement_mode = EnforcementMode(mode_str)
    except ValueError:
        _enforcement_mode = EnforcementMode.AUDIT

    return _enforcement_mode


def set_enforcement_mode(mode: EnforcementMode) -> None:
    """Set the enforcement mode programmatically.

    The policy loader calls this with the backend-supplied mode on each
    fetch so the evaluator picks up the platform-controlled value.
    """
    global _enforcement_mode
    _enforcement_mode = mode


def reset_enforcement_mode() -> None:
    """Clear cached enforcement mode (intended for tests)."""
    global _enforcement_mode
    _enforcement_mode = None
