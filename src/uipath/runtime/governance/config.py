"""Runtime-level governance enforcement-mode state.

The feature-flag gate (``is_governance_enabled``) lives in
:mod:`uipath.core.governance.config` because it is process-level and
must be resolvable by callers that do not depend on
``uipath-runtime``. The enforcement mode is *per-policy* — set by the
backend on each policy fetch via the ``/runtime/policy`` endpoint —
and therefore lives here in the runtime package alongside the policy
loader that applies it.
"""

from __future__ import annotations

import logging
import os
from enum import Enum

logger = logging.getLogger(__name__)

ENV_ENFORCEMENT_MODE = "UIPATH_GOVERNANCE_MODE"


class EnforcementMode(str, Enum):
    """Governance enforcement modes."""

    AUDIT = "audit"  # Evaluate and log; never block.
    ENFORCE = "enforce"  # Block on DENY rules.
    DISABLED = "disabled"  # Skip evaluation entirely.


_enforcement_mode: EnforcementMode | None = None


def get_enforcement_mode() -> EnforcementMode:
    """Return the current enforcement mode.

    The mode is cached after first read. Resolution order:

    1. A value previously set via :func:`set_enforcement_mode` (the
       policy loader calls this with the backend-supplied mode on every
       successful policy fetch — that's the canonical source).
    2. ``UIPATH_GOVERNANCE_MODE`` env var (developer override).
    3. Default :attr:`EnforcementMode.DISABLED` — skip evaluation
       entirely until the server explicitly opts the tenant in. This
       keeps empty-policy / failed-fetch / pre-fetch scenarios free of
       per-call audit overhead; a tenant with policies wins the cache
       on the first ``set_enforcement_mode`` call from the loader.
    """
    global _enforcement_mode
    if _enforcement_mode is not None:
        return _enforcement_mode

    mode_str = os.getenv(ENV_ENFORCEMENT_MODE, "disabled").lower()
    try:
        _enforcement_mode = EnforcementMode(mode_str)
    except ValueError:
        _enforcement_mode = EnforcementMode.DISABLED

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
