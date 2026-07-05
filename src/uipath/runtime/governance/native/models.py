"""Native policy model.

Rules, checks, conditions and pack indexes consumed by the native
governance evaluator.

These are the inputs of the native evaluator. The evaluator-agnostic
*output* types (``Action``, ``AuditRecord``, …) live in
:mod:`uipath.core.governance.models`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from uipath.core.governance.models import Action, LifecycleHook


class Severity(Enum):
    """Rule severity levels."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Logic(str, Enum):
    """How a check combines its conditions."""

    ALL = "all"  # AND — every condition must hold.
    ANY = "any"  # OR — any matching condition is a hit.


@dataclass
class Condition:
    """A single condition within a rule check."""

    operator: str
    field: str
    value: Any
    negate: bool = False


@dataclass
class Check:
    """A check within a rule - contains conditions and action."""

    conditions: list[Condition]
    action: Action = Action.DENY
    message: str = ""
    logic: Logic = Logic.ALL


@dataclass
class Rule:
    """A compliance rule with checks evaluated at a specific lifecycle hook."""

    rule_id: str
    name: str
    clause: str
    hook: LifecycleHook
    action: Action
    severity: Severity = Severity.HIGH
    checks: list[Check] = field(default_factory=list)
    enabled: bool = True
    description: str = ""
    pack_name: str = ""

    # Approval configuration (for ESCALATE action)
    approval_config: dict[str, Any] = field(default_factory=dict)


@dataclass
class CheckContext:
    """Context passed to rule evaluation.

    Scoped to evaluator input data only. Trace correlation is
    intentionally not carried here — that concern is owned by the
    provider / platform layer, not by the evaluator input model.
    """

    hook: LifecycleHook
    agent_name: str
    runtime_id: str

    # Content fields (populated based on hook)
    agent_input: str = ""
    agent_output: str = ""
    model_input: str = ""
    model_output: str = ""
    model_name: str = ""
    tool_name: str = ""
    tool_args: dict[str, Any] = field(default_factory=dict)
    tool_result: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)

    # Session state
    session_state: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    # Ring level (privilege level: 0=system, 1=admin, 2=user, 3=untrusted)
    ring: int = 2


@dataclass
class PolicyPack:
    """A collection of rules for a compliance standard."""

    name: str
    version: str
    description: str
    rules: list[Rule]
    enabled: bool = True


@dataclass
class PolicyIndex:
    """Index of all loaded policy packs and rules."""

    packs: dict[str, PolicyPack] = field(default_factory=dict)
    _rules_by_id: dict[str, Rule] = field(default_factory=dict)
    _rules_by_hook: dict[LifecycleHook, list[Rule]] = field(default_factory=dict)

    def add_pack(self, pack: PolicyPack) -> None:
        """Add a policy pack to the index."""
        self.packs[pack.name] = pack
        for rule in pack.rules:
            rule.pack_name = pack.name
            self._rules_by_id[rule.rule_id] = rule
            if rule.hook not in self._rules_by_hook:
                self._rules_by_hook[rule.hook] = []
            self._rules_by_hook[rule.hook].append(rule)

    def get_rule(self, rule_id: str) -> Rule | None:
        """Get a rule by ID."""
        return self._rules_by_id.get(rule_id)

    def get_rules_for_hook(self, hook: LifecycleHook) -> list[Rule]:
        """Get all rules for a lifecycle hook."""
        return self._rules_by_hook.get(hook, [])

    def get_rules_for_pack(self, pack_name: str) -> list[Rule]:
        """Get all rules for a pack."""
        pack = self.packs.get(pack_name)
        return pack.rules if pack else []

    @property
    def pack_names(self) -> list[str]:
        """Get all pack names."""
        return list(self.packs.keys())

    @property
    def total_rules(self) -> int:
        """Get total number of rules."""
        return len(self._rules_by_id)

    @property
    def all_rules(self) -> list[Rule]:
        """Get all rules."""
        return list(self._rules_by_id.values())
