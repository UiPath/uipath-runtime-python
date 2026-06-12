"""Native UiPath governance policy evaluator.

YAML-defined rules evaluated in-process at each agent lifecycle hook.
Reads policies from the UiPath governance backend
(``GET /api/v1/policy``) at startup and runs the deterministic
detectors backing ISO 42001 controls.

This subpackage owns:

- :class:`GovernanceEvaluator` – the evaluator implementation.
- The native policy model: :class:`Rule`, :class:`Check`,
  :class:`Condition`, :class:`PolicyIndex`.
- Policy fetch + YAML compilation plumbing.

Shared output types (``Action``, ``AuditRecord``, …) live in
:mod:`uipath.core.governance`.
"""

from .evaluator import GovernanceEvaluator
from .loader import (
    get_policy_index,
    load_policy_index,
    prefetch_policy_index,
    reset_policy_index,
)
from .models import (
    Check,
    CheckContext,
    Condition,
    PolicyIndex,
    PolicyPack,
    Rule,
    Severity,
)

__all__ = [
    "GovernanceEvaluator",
    # Loader
    "get_policy_index",
    "load_policy_index",
    "prefetch_policy_index",
    "reset_policy_index",
    # Native policy model
    "Check",
    "CheckContext",
    "Condition",
    "PolicyIndex",
    "PolicyPack",
    "Rule",
    "Severity",
]
