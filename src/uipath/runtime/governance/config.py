"""Runtime-level governance enforcement-mode state.

The feature-flag gate (``is_governance_enabled``) lives in
:mod:`uipath.core.governance.config` because it is process-level and
must be resolvable by callers that do not depend on
``uipath-runtime``. The enforcement mode is *per-policy* —
provider-supplied on each policy load — and therefore lives here in
the runtime package alongside the policy loader that applies it via
:func:`set_enforcement_mode`.
"""

from __future__ import annotations

# ``EnforcementMode`` is the shared governance value type; it's defined in
# uipath.core.governance (a lower abstraction level) and re-exported here so
# runtime callers keep a single import site. The per-process mode *state*
# below is runtime-owned and applied by the policy loader.
from uipath.core.governance import EnforcementMode as EnforcementMode


class _EnforcementModeState:
    """Holds the active enforcement mode.

    A single module-level instance backs the get/set/reset helpers, so the
    mode is updated by mutating an attribute rather than rebinding a module
    global. ``mode is None`` means "no provider has supplied a mode yet" —
    until then (and if the provider omits a mode) governance defaults to
    AUDIT.
    """

    def __init__(self) -> None:
        self.mode: EnforcementMode | None = None


# The enforcement mode is supplied by the policy provider on each load;
# the loader applies it via :func:`set_enforcement_mode`.
_state = _EnforcementModeState()


def get_enforcement_mode() -> EnforcementMode:
    """Return the current enforcement mode.

    The canonical source is whatever the policy provider supplied on
    the most recent load, applied via :func:`set_enforcement_mode`.
    Until that load lands (or if the provider returns no mode), the
    default is :attr:`EnforcementMode.AUDIT` — evaluate and log without
    blocking. Defaulting to AUDIT avoids the chicken-and-egg where a
    DISABLED default would short-circuit evaluation before the
    background policy load could ever opt the tenant in.
    """
    return _state.mode if _state.mode is not None else EnforcementMode.AUDIT


def set_enforcement_mode(mode: EnforcementMode) -> None:
    """Set the enforcement mode programmatically.

    The policy loader calls this with the provider-supplied mode on
    each load so the evaluator picks up the platform-controlled value.
    """
    _state.mode = mode
