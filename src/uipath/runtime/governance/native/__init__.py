"""Native UiPath governance policy evaluator.

YAML-defined rules evaluated in-process at each agent lifecycle hook.
The host fetches the policy pack via the
:class:`GovernancePolicyProvider` protocol and compiles it into a
:class:`PolicyIndex` with :func:`build_policy_index_from_yaml` *before*
constructing :class:`GovernanceRuntime` — so the runtime layer never
performs I/O at construction time.

This subpackage owns:

- :class:`GovernanceEvaluator` – the evaluator implementation.
- :func:`build_policy_index_from_yaml` – pure YAML → :class:`PolicyIndex`
  compiler.
- The native policy model: :class:`Rule`, :class:`Check`,
  :class:`Condition`, :class:`PolicyIndex`.

Shared output types (``Action``, ``AuditRecord``, …) live in
:mod:`uipath.core.governance`.
"""

from ._yaml_to_index import build_policy_index_from_yaml
from .evaluator import GovernanceEvaluator
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
    "build_policy_index_from_yaml",
    # Native policy model
    "Check",
    "CheckContext",
    "Condition",
    "PolicyIndex",
    "PolicyPack",
    "Rule",
    "Severity",
]
