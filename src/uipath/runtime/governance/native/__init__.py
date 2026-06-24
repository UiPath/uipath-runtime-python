"""Native UiPath governance policy evaluator.

YAML-defined rules evaluated in-process at each agent lifecycle hook.
Reads policies through a :class:`GovernancePolicyProvider` (the provider
owns the wire transport) and runs the deterministic detectors backing
ISO 42001 controls.

This subpackage owns:

- :class:`GovernanceEvaluator` – the evaluator implementation.
- :class:`PolicyLoader` – the instance-scoped policy cache + prefetch.
- The native policy model: :class:`Rule`, :class:`Check`,
  :class:`Condition`, :class:`PolicyIndex`.

Shared output types (``Action``, ``AuditRecord``, …) live in
:mod:`uipath.core.governance`.
"""

from .evaluator import GovernanceEvaluator
from .loader import PolicyLoader
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
    "PolicyLoader",
    # Native policy model
    "Check",
    "CheckContext",
    "Condition",
    "PolicyIndex",
    "PolicyPack",
    "Rule",
    "Severity",
]
