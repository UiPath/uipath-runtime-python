"""Shared test-only helpers.

Keeps test concerns out of the production governance package: the
enforcement-mode reset used for per-test isolation lives here rather than
in :mod:`uipath.runtime.governance.config`.
"""

from __future__ import annotations

from uipath.runtime.governance import config


def reset_enforcement_mode() -> None:
    """Clear the process-wide enforcement mode so the AUDIT default re-applies.

    Test isolation only — production code never resets the mode; the policy
    loader sets it from the backend ``/runtime/policy`` response.
    """
    config._state.mode = None