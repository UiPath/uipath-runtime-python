"""Native UiPath governance policy evaluator.

Rules evaluated in-process at each agent lifecycle hook. The caller
fetches the policy pack and compiles it into a :class:`PolicyIndex`
outside this package — the runtime never sees the wire format. Any
provider, any code path, any format is fair game; this package only
consumes the compiled index.

This subpackage owns:

- :class:`GovernanceEvaluator` – the evaluator implementation.
- The native policy model: :class:`Rule`, :class:`Check`,
  :class:`Condition`, :class:`PolicyIndex`, :class:`PolicyPack`.

Shared output types (``Action``, ``AuditRecord``, …) live in
:mod:`uipath.core.governance`.
"""

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
    # Native policy model
    "Check",
    "CheckContext",
    "Condition",
    "PolicyIndex",
    "PolicyPack",
    "Rule",
    "Severity",
]
