"""Runtime-level governance enforcement-mode state.

The feature-flag gate (``is_governance_enabled``) lives in
:mod:`uipath.core.governance.config` because it is process-level and
must be resolvable by callers that do not depend on
``uipath-runtime``. The enforcement mode is *per-policy* — owned by the
backend and delivered on each policy fetch via the ``/runtime/policy``
endpoint — and therefore lives here in the runtime package alongside the
policy loader that applies it.
"""

from __future__ import annotations

from enum import Enum


class EnforcementMode(str, Enum):
    """Governance enforcement modes."""

    AUDIT = "audit"  # Evaluate and log; never block.
    ENFORCE = "enforce"  # Block on DENY rules.
    DISABLED = "disabled"  # Skip evaluation entirely.


class _EnforcementModeState:
    """Holds the active enforcement mode.

    A single module-level instance backs the get/set/reset helpers, so the
    mode is updated by mutating an attribute rather than rebinding a module
    global. ``mode is None`` means "not yet set by the backend" — until
    then (and if the backend omits a mode) governance defaults to AUDIT.
    """

    def __init__(self) -> None:
        self.mode: EnforcementMode | None = None


# The enforcement mode is owned by the backend: the policy loader applies
# the mode from the ``/runtime/policy`` response via
# :func:`set_enforcement_mode`.
_state = _EnforcementModeState()


def get_enforcement_mode() -> EnforcementMode:
    """Return the current enforcement mode.

    The canonical source is the backend ``/runtime/policy`` response,
    applied by the policy loader via :func:`set_enforcement_mode`. Until
    that fetch lands (or if the backend returns no mode), the default is
    :attr:`EnforcementMode.AUDIT` — evaluate and log without blocking.
    Defaulting to AUDIT avoids the chicken-and-egg where a DISABLED
    default would short-circuit evaluation before the background policy
    fetch could ever opt the tenant in.
    """
    return _state.mode if _state.mode is not None else EnforcementMode.AUDIT


def set_enforcement_mode(mode: EnforcementMode) -> None:
    """Set the enforcement mode programmatically.

    The policy loader calls this with the backend-supplied mode on each
    fetch so the evaluator picks up the platform-controlled value.
    """
    _state.mode = mode