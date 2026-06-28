"""Runtime YAML → PolicyIndex parser.

Mirrors the shape produced by ``packs/compile_packs.py`` but builds
the :class:`PolicyIndex` directly from parsed YAML data rather than
generating Python source. The host calls this to compile the YAML
body returned by :meth:`GovernancePolicyProvider.get_policy_async`
into an in-memory index, then hands the index to
:class:`GovernanceRuntime`.

Accepts either a single YAML document (one pack) or a multi-document
stream (``---``-separated packs). Unknown check types and malformed
rules are skipped with a warning — partial packs are preferred over
failing the whole load.
"""

from __future__ import annotations

import logging
from typing import Any

import yaml
from uipath.core.governance.models import Action, LifecycleHook

from uipath.runtime.governance.native.models import (
    Check,
    Condition,
    Logic,
    PolicyIndex,
    PolicyPack,
    Rule,
    Severity,
)

logger = logging.getLogger(__name__)


_HOOK_MAP: dict[str, LifecycleHook] = {
    "before_agent": LifecycleHook.BEFORE_AGENT,
    "after_agent": LifecycleHook.AFTER_AGENT,
    "before_model": LifecycleHook.BEFORE_MODEL,
    "after_model": LifecycleHook.AFTER_MODEL,
    "wrap_tool_call": LifecycleHook.TOOL_CALL,
    "tool_call": LifecycleHook.TOOL_CALL,
    "after_tool": LifecycleHook.AFTER_TOOL,
}

_ACTION_MAP: dict[str, Action] = {
    "block": Action.DENY,
    "deny": Action.DENY,
    "log": Action.AUDIT,
    "audit": Action.AUDIT,
    "allow": Action.ALLOW,
    "require_approval": Action.ESCALATE,
    "escalate": Action.ESCALATE,
}

_SEVERITY_MAP: dict[str, Severity] = {
    "low": Severity.LOW,
    "medium": Severity.MEDIUM,
    "high": Severity.HIGH,
    "critical": Severity.CRITICAL,
}


def build_policy_index_from_yaml(yaml_text: str) -> PolicyIndex:
    """Parse YAML policy packs into a PolicyIndex.

    Args:
        yaml_text: YAML body, either a single document or ``---``-separated
            multi-document stream. Each document is one pack.

    Returns:
        PolicyIndex with all successfully parsed packs added. Empty when
        the input has no parseable packs.

    Raises:
        yaml.YAMLError: If the YAML itself is malformed. Callers are
            expected to fall back to the compiled index on this error.
    """
    index = PolicyIndex()
    documents = list(yaml.safe_load_all(yaml_text))

    for doc in documents:
        if not isinstance(doc, dict):
            continue
        pack = _build_pack(doc)
        if pack is not None and pack.rules:
            index.add_pack(pack)

    logger.debug(
        "Built PolicyIndex from YAML: packs=%s, rules=%d",
        index.pack_names,
        index.total_rules,
    )
    return index


def _build_pack(data: dict[str, Any]) -> PolicyPack | None:
    """Build a PolicyPack from one YAML document."""
    name = data.get("standard") or data.get("name")
    if not name:
        logger.warning("Skipping pack: missing 'standard'/'name' field")
        return None

    default_action_str = data.get("default_action", "block")
    default_action = _ACTION_MAP.get(default_action_str, Action.DENY)

    rules: list[Rule] = []
    for i, rule_data in enumerate(data.get("rules", []) or []):
        if not isinstance(rule_data, dict):
            continue
        rule = _build_rule(rule_data, default_action, i)
        if rule is not None:
            rules.append(rule)

    return PolicyPack(
        name=str(name),
        version=str(data.get("version", "1.0.0")),
        description=str(data.get("description", "")),
        rules=rules,
    )


def _build_rule(
    data: dict[str, Any], default_action: Action, index: int
) -> Rule | None:
    """Build a single Rule from a YAML rule entry."""
    hook = _HOOK_MAP.get(data.get("hook", "before_model"))
    if hook is None:
        logger.warning(
            "Skipping rule %s: unknown hook %r", data.get("id"), data.get("hook")
        )
        return None

    action_str = data.get("action")
    action = (
        _ACTION_MAP.get(action_str, default_action) if action_str else default_action
    )

    default_sev = "high" if action == Action.DENY else "medium"
    severity = _SEVERITY_MAP.get(data.get("severity", default_sev), Severity.HIGH)

    checks = _build_checks(
        data.get("checks", []) or [],
        action,
        mapped_to_uipath=bool(data.get("mapped_to_uipath", False)),
        policy_enabled=bool(data.get("policy_enabled", True)),
    )

    # If checks were declared but none could be parsed (e.g. all unknown
    # types), skip the rule. A rule with zero checks "always matches" in
    # the evaluator, so keeping it would make it fire on every request.
    declared = data.get("checks", []) or []
    if declared and not checks:
        logger.warning(
            "Skipping rule %s: none of its %d declared check(s) could be parsed",
            data.get("id"),
            len(declared),
        )
        return None

    return Rule(
        rule_id=str(data.get("id", f"RULE-{index}")),
        name=str(data.get("name", data.get("id", f"RULE-{index}"))),
        clause=str(data.get("clause", data.get("owasp_ref", ""))),
        hook=hook,
        action=action,
        severity=severity,
        checks=checks,
        enabled=bool(data.get("enabled", True)),
        description=str(data.get("description", "")),
    )


def _build_checks(
    checks_data: list[dict[str, Any]],
    default_action: Action,
    *,
    mapped_to_uipath: bool = False,
    policy_enabled: bool = True,
) -> list[Check]:
    """Build the checks list for a rule.

    ``mapped_to_uipath`` / ``policy_enabled`` are rule-level flags read
    by ``guardrail_fallback`` checks so the per-check condition can
    decide whether to fire the compensating governance call.
    """
    checks: list[Check] = []
    for check_data in checks_data:
        if not isinstance(check_data, dict):
            continue
        check = _build_check(
            check_data,
            default_action,
            mapped_to_uipath=mapped_to_uipath,
            policy_enabled=policy_enabled,
        )
        if check is not None:
            checks.append(check)
    return checks


def _build_check(
    data: dict[str, Any],
    default_action: Action,
    *,
    mapped_to_uipath: bool = False,
    policy_enabled: bool = True,
) -> Check | None:
    """Build one Check from a YAML check entry.

    Supports the same check types as ``compile_packs.py``: explicit
    conditions, regex, budget, tool_allowlist, parameter_validation,
    rate_limit, field_regex, sentiment_concern, data_quality_score,
    incident_taxonomy, commitment_extractor, plus ``guardrail_fallback``
    (reads the rule-level ``mapped_to_uipath`` / ``policy_enabled`` flags
    threaded in from ``_build_rule``).
    """
    conditions: list[Condition] = []
    message = ""

    raw_conditions = data.get("conditions")
    has_explicit_conditions = (
        isinstance(raw_conditions, list)
        and raw_conditions
        and isinstance(raw_conditions[0], dict)
        and "operator" in raw_conditions[0]
    )

    check_type = data.get("type", "regex")

    if has_explicit_conditions:
        assert isinstance(raw_conditions, list)  # narrowed by has_explicit_conditions
        conditions.extend(_make_conditions(raw_conditions))
        message = str(data.get("message", ""))

    elif check_type == "regex":
        patterns = data.get("patterns", []) or []
        scope = data.get("scope", ["human", "ai"])
        field = _field_for_scope(scope)
        for pattern in patterns:
            conditions.append(Condition(operator="regex", field=field, value=pattern))
        message = f"Pattern matched in {scope}"

    elif check_type == "budget":
        if "max_tool_calls_per_session" in data:
            conditions.append(
                Condition(
                    operator="gt",
                    field="session_state.tool_calls",
                    value=data["max_tool_calls_per_session"],
                )
            )
        if "max_tool_calls_per_minute" in data:
            conditions.append(
                Condition(
                    operator="gt",
                    field="session_state.tool_calls_per_minute",
                    value=data["max_tool_calls_per_minute"],
                )
            )
        if "max_consecutive_tool_calls" in data:
            conditions.append(
                Condition(
                    operator="gt",
                    field="session_state.consecutive_tool_calls",
                    value=data["max_consecutive_tool_calls"],
                )
            )
        message = "Tool budget exceeded"

    elif check_type == "tool_allowlist":
        blocked_tools = data.get("blocked_tools", []) or []
        if blocked_tools:
            conditions.append(
                Condition(operator="in_list", field="tool_name", value=blocked_tools)
            )
        message = "Tool not allowed"

    elif check_type == "parameter_validation":
        for pattern in data.get("additional_patterns", []) or []:
            conditions.append(
                Condition(operator="regex", field="tool_args", value=pattern)
            )
        message = "Suspicious pattern in tool parameters"

    elif check_type == "rate_limit":
        if "max_llm_calls_per_session" in data:
            conditions.append(
                Condition(
                    operator="gt",
                    field="session_state.llm_calls",
                    value=data["max_llm_calls_per_session"],
                )
            )
        if "max_llm_calls_per_minute" in data:
            conditions.append(
                Condition(
                    operator="gt",
                    field="session_state.llm_calls_per_minute",
                    value=data["max_llm_calls_per_minute"],
                )
            )
        message = "Rate limit exceeded"

    elif check_type == "field_regex":
        conditions.extend(_make_conditions(data.get("conditions", []) or []))
        message = str(data.get("message", "Field regex check failed"))

    elif check_type == "data_quality_score":
        field = data.get("field", "tool_result")
        if data.get("check_encoding", True):
            conditions.append(
                Condition(
                    operator="encoding_concern",
                    field=field,
                    value={
                        "min_confidence": float(data.get("min_confidence", 0.5)),
                        "max_replacement_ratio": float(
                            data.get("max_replacement_ratio", 0.05)
                        ),
                        "min_corruption_events": int(
                            data.get("min_corruption_events", 2)
                        ),
                    },
                )
            )
        if data.get("check_entropy", True):
            conditions.append(
                Condition(
                    operator="entropy_concern",
                    field=field,
                    value={
                        "min": float(data.get("entropy_min", 1.5)),
                        "max": float(data.get("entropy_max", 7.5)),
                    },
                )
            )
        message = str(data.get("message", ""))

    elif check_type == "incident_taxonomy":
        field = data.get("field", "model_output")
        categories = data.get("categories")
        value: dict[str, Any] = {}
        if categories:
            value["categories"] = list(categories)
        conditions.append(
            Condition(operator="incident_concern", field=field, value=value)
        )
        message = str(data.get("message", ""))

    elif check_type == "commitment_extractor":
        field = data.get("field", "model_output")
        conditions.append(
            Condition(
                operator="commitment_concern",
                field=field,
                value={
                    "require_amount": bool(data.get("require_amount", True)),
                    "require_deadline": bool(data.get("require_deadline", False)),
                },
            )
        )
        message = str(data.get("message", ""))

    elif check_type == "sentiment_concern":
        field = data.get("field", "model_input")
        threshold = float(data.get("threshold", -0.3))
        conditions.append(
            Condition(
                operator="vader_concern",
                field=field,
                value={"threshold": threshold},
            )
        )
        message = str(
            data.get(
                "message",
                f"Negative sentiment detected (VADER compound <= {threshold})",
            )
        )

    elif check_type == "guardrail_fallback":
        # Centralized guardrail compensating control. The on/off state
        # lives at the RULE level (mapped_to_uipath / policy_enabled),
        # threaded in from ``_build_rule``; ``validator`` names which
        # guardrail check the server should run on behalf of the agent.
        # The condition matches only when the guardrail is mapped to
        # UiPath but disabled — see the ``guardrail_fallback`` operator
        # in :class:`GovernanceEvaluator`.
        conditions.append(
            Condition(
                operator="guardrail_fallback",
                field="",
                value={
                    "validator": str(data.get("validator", "")),
                    "mapped_to_uipath": mapped_to_uipath,
                    "policy_enabled": policy_enabled,
                },
            )
        )
        message = str(
            data.get("message", "Guardrail disabled — compensating check needed.")
        )

    else:
        logger.debug("Skipping check: unknown type %r", check_type)
        return None

    if not conditions:
        return None

    action_str = data.get("action")
    action = (
        _ACTION_MAP.get(action_str, default_action) if action_str else default_action
    )

    message = str(data.get("message", message))

    # Multi-PATTERN shorthand (regex/parameter_validation expanded from
    # several patterns for one concept) defaults to OR — any pattern
    # hitting is a match. An explicit `conditions:` list defaults to AND
    # (all must hold) and must NOT inherit the pattern-shorthand OR even
    # though `check_type` falls back to "regex". Explicit `logic` wins.
    if (
        not has_explicit_conditions
        and check_type in ("parameter_validation", "regex")
        and len(conditions) > 1
    ):
        default_logic = "any"
    else:
        default_logic = "all"
    logic_str = str(data.get("logic", default_logic)).lower()
    try:
        logic = Logic(logic_str)
    except ValueError:
        logic = Logic.ALL

    return Check(conditions=conditions, action=action, message=message, logic=logic)


def _make_conditions(raw: list[dict[str, Any]]) -> list[Condition]:
    """Translate a list of YAML condition dicts into Condition objects."""
    out: list[Condition] = []
    for cond in raw:
        if not isinstance(cond, dict):
            continue
        out.append(
            Condition(
                operator=str(cond.get("operator", "regex")),
                field=str(cond.get("field", "model_input")),
                value=cond.get("value", ""),
                negate=bool(cond.get("negate", False)),
            )
        )
    return out


def _field_for_scope(scope: list[str] | str) -> str:
    """Map a YAML `scope` value to the CheckContext field it targets."""
    if isinstance(scope, str):
        scope = [scope]
    if "system" in scope or "human" in scope:
        return "model_input"
    if "ai" in scope:
        return "model_output"
    if "tool_result" in scope:
        return "tool_result"
    return "model_input"
