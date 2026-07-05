"""Tests for ``GovernanceEvaluator`` operators and field resolution.

Covers each operator implemented in :meth:`_apply_operator` plus the
``_check_*`` helper functions (vader, encoding, entropy, incident,
commitment) and the ``evaluate_*`` dispatchers.
"""

from __future__ import annotations

from typing import Any

import pytest
from uipath.core.governance import EnforcementMode
from uipath.core.governance.models import Action, LifecycleHook

from uipath.runtime.governance.native.evaluator import (
    _INCIDENT_PATTERNS,
    GovernanceEvaluator,
)
from uipath.runtime.governance.native.models import (
    Check,
    CheckContext,
    Condition,
    Logic,
    PolicyIndex,
    PolicyPack,
    Rule,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _evaluator() -> GovernanceEvaluator:
    """Build a GovernanceEvaluator with an empty PolicyIndex (operators only).

    AUDIT is the default mode; operator tests don't care about
    enforcement and we don't need an audit manager for purely
    operator-level assertions.
    """
    return GovernanceEvaluator(policy_index=PolicyIndex())


def _ctx(**fields: Any) -> CheckContext:
    """Construct a CheckContext with sensible defaults plus overrides."""
    defaults: dict[str, Any] = dict(
        hook=LifecycleHook.AFTER_MODEL,
        agent_name="agent",
        runtime_id="rt-1",
    )
    defaults.update(fields)
    return CheckContext(**defaults)


def _rule_with_condition(
    operator: str, field: str, value, *, negate: bool = False
) -> Rule:
    return Rule(
        rule_id="r1",
        name="r1",
        clause="",
        hook=LifecycleHook.AFTER_MODEL,
        action=Action.AUDIT,
        checks=[
            Check(
                conditions=[
                    Condition(
                        operator=operator, field=field, value=value, negate=negate
                    )
                ],
            )
        ],
    )


# Mode is per-instance now — tests construct evaluators with the mode
# they need via the ``enforcement_mode`` kwarg. No process-globals to
# reset.


# ---------------------------------------------------------------------------
# Field resolution — _get_field_value
# ---------------------------------------------------------------------------


def test_get_field_value_top_level_attr() -> None:
    ev = _evaluator()
    ctx = _ctx(model_output="hello")
    assert ev._get_field_value("model_output", ctx) == "hello"


def test_get_field_value_dotted_path_into_dict() -> None:
    ev = _evaluator()
    ctx = _ctx(session_state={"tool_calls": 7})
    assert ev._get_field_value("session_state.tool_calls", ctx) == 7


def test_get_field_value_missing_segment_returns_none() -> None:
    ev = _evaluator()
    ctx = _ctx()
    assert ev._get_field_value("nonexistent", ctx) is None
    assert ev._get_field_value("session_state.absent", ctx) is None


# ---------------------------------------------------------------------------
# Existence / guardrail_fallback (special-cased before the None check)
# ---------------------------------------------------------------------------


def test_exists_true_when_value_present() -> None:
    ev = _evaluator()
    ctx = _ctx(model_output="x")
    assert (
        ev._apply_operator("exists", ev._get_field_value("model_output", ctx), None)
        is True
    )


def test_exists_false_when_missing() -> None:
    ev = _evaluator()
    assert ev._apply_operator("exists", None, None) is False


def test_not_exists_inverse() -> None:
    ev = _evaluator()
    assert ev._apply_operator("not_exists", None, None) is True
    assert ev._apply_operator("not_exists", "x", None) is False


def test_guardrail_fallback_mapped_and_disabled_fires() -> None:
    ev = _evaluator()
    result = ev._apply_operator(
        "guardrail_fallback",
        None,
        {"mapped_to_uipath": True, "policy_enabled": False, "validator": "pii"},
    )
    assert result is True


@pytest.mark.parametrize(
    "cfg",
    [
        {"mapped_to_uipath": False, "policy_enabled": False},
        {"mapped_to_uipath": True, "policy_enabled": True},
        {"mapped_to_uipath": False, "policy_enabled": True},
    ],
)
def test_guardrail_fallback_silent_when_not_mapped_or_enabled(
    cfg: dict[str, Any],
) -> None:
    ev = _evaluator()
    assert ev._apply_operator("guardrail_fallback", None, cfg) is False


def test_guardrail_fallback_non_dict_value_silent() -> None:
    ev = _evaluator()
    assert ev._apply_operator("guardrail_fallback", None, "string") is False


# ---------------------------------------------------------------------------
# None-field short-circuit (everything except exists / guardrail_fallback)
# ---------------------------------------------------------------------------


def test_other_operators_short_circuit_when_field_is_none() -> None:
    ev = _evaluator()
    for op in ("contains", "regex", "in_list", "gt"):
        assert ev._apply_operator(op, None, "anything") is False, op


# ---------------------------------------------------------------------------
# Numeric operators
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "op,lhs,rhs,expected",
    [
        ("gt", 5, 3, True),
        ("gt", 3, 5, False),
        ("gt", 3, 3, False),
        ("gte", 3, 3, True),
        ("gte", 2, 3, False),
        ("lt", 1, 3, True),
        ("lt", 3, 3, False),
        ("lte", 3, 3, True),
        ("lte", 4, 3, False),
    ],
)
def test_numeric_operators(op: str, lhs: float, rhs: float, expected: bool) -> None:
    assert _evaluator()._apply_operator(op, lhs, rhs) is expected


def test_numeric_operators_handle_string_coercion() -> None:
    ev = _evaluator()
    assert ev._apply_operator("gt", "5", "3") is True


def test_numeric_operators_return_false_on_uncoercible() -> None:
    ev = _evaluator()
    assert ev._apply_operator("gt", "not-a-number", 3) is False
    assert ev._apply_operator("gt", 3, "not-a-number") is False


# ---------------------------------------------------------------------------
# String operators
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "op,lhs,rhs,expected",
    [
        ("equals", "abc", "abc", True),
        ("equals", "abc", "ABC", False),  # equals is case-sensitive
        ("eq", "x", "x", True),
        ("not_equals", "abc", "xyz", True),
        ("ne", "x", "x", False),
        ("contains", "Hello World", "world", True),  # case-insensitive
        ("contains", "Hello", "xyz", False),
        ("not_contains", "Hello", "xyz", True),
        ("not_contains", "Hello", "hello", False),
    ],
)
def test_string_operators(op: str, lhs: str, rhs: str, expected: bool) -> None:
    assert _evaluator()._apply_operator(op, lhs, rhs) is expected


def test_regex_matches_pattern() -> None:
    ev = _evaluator()
    assert ev._apply_operator("regex", "Cost: $1,200", r"\$\d+") is True


def test_regex_matches_alias() -> None:
    """``matches`` is documented as a synonym for ``regex``."""
    ev = _evaluator()
    assert ev._apply_operator("matches", "abc-123", r"\d+") is True


def test_regex_invalid_pattern_returns_false() -> None:
    """Malformed regex is logged and silently returns False."""
    ev = _evaluator()
    assert ev._apply_operator("regex", "anything", "(unclosed") is False


# ---------------------------------------------------------------------------
# List operators
# ---------------------------------------------------------------------------


def test_in_list_membership() -> None:
    ev = _evaluator()
    assert (
        ev._apply_operator("in_list", "delete_file", ["shell", "delete_file"]) is True
    )
    assert ev._apply_operator("in_list", "ls", ["shell", "delete_file"]) is False


def test_in_list_non_list_value_returns_false() -> None:
    ev = _evaluator()
    assert ev._apply_operator("in_list", "x", "not a list") is False


def test_not_in_list_inverse() -> None:
    ev = _evaluator()
    assert ev._apply_operator("not_in_list", "ls", ["shell"]) is True
    assert ev._apply_operator("not_in_list", "shell", ["shell"]) is False


def test_not_in_list_non_list_value_returns_true() -> None:
    """``not_in_list`` against a non-list value safely returns True
    (nothing is in a non-list)."""
    ev = _evaluator()
    assert ev._apply_operator("not_in_list", "x", "not a list") is True


# ---------------------------------------------------------------------------
# Unknown operator
# ---------------------------------------------------------------------------


def test_unknown_operator_returns_false() -> None:
    """Unknown operator strings log a debug message and return False."""
    ev = _evaluator()
    assert ev._apply_operator("never_heard_of_this", "x", "y") is False


# ---------------------------------------------------------------------------
# Negate flag — flips the result
# ---------------------------------------------------------------------------


def test_condition_negate_flips_result() -> None:
    ev = _evaluator()
    ctx = _ctx(model_output="hello")
    # contains "hello" → matches; negate inverts to False.
    cond = Condition(
        operator="contains",
        field="model_output",
        value="hello",
        negate=True,
    )
    assert ev._evaluate_condition(cond, ctx) is False
    cond2 = Condition(
        operator="contains",
        field="model_output",
        value="world",
        negate=True,
    )
    assert ev._evaluate_condition(cond2, ctx) is True


# ---------------------------------------------------------------------------
# Check-level logic: "all" (AND) vs "any" (OR), and empty-conditions
# ---------------------------------------------------------------------------


def test_empty_check_conditions_always_match() -> None:
    """A check with no conditions trivially matches — surfaces rule shape bugs."""
    ev = _evaluator()
    check = Check(conditions=[], logic=Logic.ALL)
    matched, _ = ev._evaluate_check(check, _ctx())
    assert matched is True


def test_check_logic_all_requires_every_condition() -> None:
    ev = _evaluator()
    check = Check(
        conditions=[
            Condition(operator="contains", field="model_output", value="a"),
            Condition(operator="contains", field="model_output", value="missing"),
        ],
        logic=Logic.ALL,
    )
    matched, _ = ev._evaluate_check(check, _ctx(model_output="a only"))
    assert matched is False


def test_check_logic_any_requires_one_condition() -> None:
    ev = _evaluator()
    check = Check(
        conditions=[
            Condition(operator="contains", field="model_output", value="present"),
            Condition(operator="contains", field="model_output", value="absent"),
        ],
        logic=Logic.ANY,
    )
    matched, detail = ev._evaluate_check(check, _ctx(model_output="present text"))
    assert matched is True
    # detail is the check's message on match; empty by default in our builder.
    assert detail == ""


# ---------------------------------------------------------------------------
# VADER sentiment
# ---------------------------------------------------------------------------


def test_vader_concern_negative_text_fires() -> None:
    """A clearly-negative sentence trips the default threshold of -0.3."""
    assert (
        GovernanceEvaluator._check_vader_concern(
            "I absolutely hate this terrible, awful product.", {"threshold": -0.3}
        )
        is True
    )


def test_vader_concern_positive_text_does_not_fire() -> None:
    assert (
        GovernanceEvaluator._check_vader_concern(
            "This is wonderful and I love it!", {"threshold": -0.3}
        )
        is False
    )


def test_vader_concern_empty_text_silent() -> None:
    assert GovernanceEvaluator._check_vader_concern("", {}) is False
    assert GovernanceEvaluator._check_vader_concern("   ", {}) is False


def test_vader_concern_threshold_as_scalar() -> None:
    """``params`` may be a bare number; the operator coerces."""
    assert GovernanceEvaluator._check_vader_concern("I hate everything", -0.3) is True


def test_vader_concern_invalid_threshold_falls_back() -> None:
    """Non-numeric scalar params fall back to the documented default."""
    # "garbage" -> default -0.3 → should still classify clear negative
    assert (
        GovernanceEvaluator._check_vader_concern(
            "I hate this awful, terrible thing", "garbage"
        )
        is True
    )


# ---------------------------------------------------------------------------
# Encoding integrity
# ---------------------------------------------------------------------------


def test_encoding_concern_clean_text_silent() -> None:
    assert (
        GovernanceEvaluator._check_encoding_concern(
            "Just a normal English sentence with no corruption.", {}
        )
        is False
    )


def test_encoding_concern_empty_silent() -> None:
    assert GovernanceEvaluator._check_encoding_concern("", {}) is False


def test_encoding_concern_replacement_chars_fire() -> None:
    """U+FFFD replacement chars are a strong corruption signal."""
    text = "Hello � � world"
    assert (
        GovernanceEvaluator._check_encoding_concern(text, {"min_corruption_events": 2})
        is True
    )


def test_encoding_concern_mojibake_bigrams_fire() -> None:
    """Latin-1-as-UTF-8 mojibake patterns are a known corruption shape."""
    text = "Ã© Ã© hello Ã©"
    assert (
        GovernanceEvaluator._check_encoding_concern(text, {"min_corruption_events": 2})
        is True
    )


def test_encoding_concern_hex_escape_literals_fire() -> None:
    """Literal ``\\xHH`` sequences mean raw bytes leaked into a string."""
    text = r"Hello \x80 \x81 \x82 world"
    assert (
        GovernanceEvaluator._check_encoding_concern(text, {"min_corruption_events": 2})
        is True
    )


# ---------------------------------------------------------------------------
# Entropy (stdlib only — deterministic)
# ---------------------------------------------------------------------------


def test_entropy_concern_normal_english_does_not_fire() -> None:
    """English prose entropy lands ~3.5–4.5 bits/byte — inside default range."""
    text = "The quick brown fox jumps over the lazy dog." * 5
    assert (
        GovernanceEvaluator._check_entropy_concern(text, {"min": 1.5, "max": 7.5})
        is False
    )


def test_entropy_concern_low_entropy_fires() -> None:
    """Highly repetitive text approaches 0 bits/byte."""
    text = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert (
        GovernanceEvaluator._check_entropy_concern(text, {"min": 1.5, "max": 7.5})
        is True
    )


def test_entropy_concern_high_entropy_fires() -> None:
    """Random-ish bytes approach 8 bits/byte."""
    # Build text with many distinct chars to push entropy high.
    text = "".join(chr(c) for c in range(32, 127)) * 5
    assert (
        GovernanceEvaluator._check_entropy_concern(text, {"min": 1.5, "max": 6.0})
        is True
    )


def test_entropy_concern_empty_silent() -> None:
    assert GovernanceEvaluator._check_entropy_concern("", {}) is False


def test_entropy_concern_non_dict_params_uses_defaults() -> None:
    """Non-dict params don't crash; defaults apply."""
    # Normal English prose still won't trip the default min=1.5, max=7.5 range.
    text = "The quick brown fox jumps over the lazy dog."
    assert GovernanceEvaluator._check_entropy_concern(text, "garbage") is False


# ---------------------------------------------------------------------------
# Incident taxonomy (regex-based, deterministic)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected_category",
    [
        ("I cannot help with that.", "safety_refusal"),
        ("I'm sorry, but I cannot answer.", "safety_refusal"),
        ("500 internal server error", "tool_failure"),
        ("Connection refused", "tool_failure"),
        ("timed out", "tool_failure"),
        ("401 unauthorized", "auth_failure"),
        ("authentication failed", "auth_failure"),
        ("429", "quota_exceeded"),
        ("rate limit exceeded", "quota_exceeded"),
        ("I made that up", "hallucination"),
        ("I don't actually know", "hallucination"),
    ],
)
def test_incident_concern_categorical_matches(
    text: str, expected_category: str
) -> None:
    """Each category in ``_INCIDENT_PATTERNS`` has at least one matching exemplar."""
    assert expected_category in _INCIDENT_PATTERNS
    assert GovernanceEvaluator._check_incident_concern(text, {}) is True


def test_incident_concern_unmatched_silent() -> None:
    assert (
        GovernanceEvaluator._check_incident_concern(
            "All systems operating normally.", {}
        )
        is False
    )


def test_incident_concern_empty_silent() -> None:
    assert GovernanceEvaluator._check_incident_concern("", {}) is False


def test_incident_concern_category_filter() -> None:
    """Limit scanning to a subset of categories via ``categories`` param."""
    # "401 unauthorized" hits auth_failure; with only quota_exceeded enabled,
    # the scanner should miss it.
    assert (
        GovernanceEvaluator._check_incident_concern(
            "401 unauthorized", {"categories": ["quota_exceeded"]}
        )
        is False
    )
    # With auth_failure enabled, it fires.
    assert (
        GovernanceEvaluator._check_incident_concern(
            "401 unauthorized", {"categories": ["auth_failure"]}
        )
        is True
    )


def test_incident_concern_unknown_category_silently_dropped() -> None:
    """Categories the system doesn't know about are silently ignored."""
    # Only the unknown category is requested — falls back to no categories,
    # so even matching text doesn't fire.
    result = GovernanceEvaluator._check_incident_concern(
        "401 unauthorized", {"categories": ["unknown_cat_xyz"]}
    )
    assert result is False


# ---------------------------------------------------------------------------
# evaluate_* dispatchers — verify they build the right CheckContext
# ---------------------------------------------------------------------------


def _record_context_evaluator() -> tuple[GovernanceEvaluator, dict[str, Any]]:
    """Patch evaluate() to capture the context it receives instead of running rules."""
    captured: dict[str, Any] = {}
    ev = _evaluator()

    def _fake_evaluate(ctx: CheckContext) -> Any:
        captured["ctx"] = ctx
        from datetime import datetime, timezone

        from uipath.core.governance.models import AuditRecord

        return AuditRecord(
            timestamp=datetime.now(timezone.utc),
            agent_name=ctx.agent_name,
            runtime_id=ctx.runtime_id,
            hook=ctx.hook,
            evaluations=[],
            final_action=Action.ALLOW,
        )

    ev.evaluate = _fake_evaluate  # type: ignore[assignment]
    return ev, captured


def test_evaluate_before_agent_builds_context() -> None:
    ev, captured = _record_context_evaluator()
    ev.evaluate_before_agent(
        agent_input="user-text",
        agent_name="a",
        runtime_id="r",
        model_name="gpt-5",
    )
    ctx = captured["ctx"]
    assert ctx.hook == LifecycleHook.BEFORE_AGENT
    assert ctx.agent_input == "user-text"
    assert ctx.model_name == "gpt-5"


def test_evaluate_after_agent_builds_context() -> None:
    ev, captured = _record_context_evaluator()
    ev.evaluate_after_agent(
        agent_output="reply",
        agent_name="a",
        runtime_id="r",
    )
    ctx = captured["ctx"]
    assert ctx.hook == LifecycleHook.AFTER_AGENT
    assert ctx.agent_output == "reply"


def test_evaluate_before_model_carries_messages() -> None:
    ev, captured = _record_context_evaluator()
    ev.evaluate_before_model(
        model_input="prompt",
        agent_name="a",
        runtime_id="r",
        messages=[{"role": "user", "content": "hi"}],
        model_name="gpt-5",
    )
    ctx = captured["ctx"]
    assert ctx.hook == LifecycleHook.BEFORE_MODEL
    assert ctx.model_input == "prompt"
    assert ctx.messages == [{"role": "user", "content": "hi"}]


def test_evaluate_after_model_builds_context() -> None:
    ev, captured = _record_context_evaluator()
    ev.evaluate_after_model(
        model_output="resp",
        agent_name="a",
        runtime_id="r",
    )
    ctx = captured["ctx"]
    assert ctx.hook == LifecycleHook.AFTER_MODEL
    assert ctx.model_output == "resp"


def test_evaluate_tool_call_carries_args() -> None:
    ev, captured = _record_context_evaluator()
    ev.evaluate_tool_call(
        tool_name="search",
        tool_args={"q": "x"},
        agent_name="a",
        runtime_id="r",
        session_state={"tool_calls": 1},
    )
    ctx = captured["ctx"]
    assert ctx.hook == LifecycleHook.TOOL_CALL
    assert ctx.tool_name == "search"
    assert ctx.tool_args == {"q": "x"}
    assert ctx.session_state == {"tool_calls": 1}


def test_evaluate_after_tool_carries_result() -> None:
    ev, captured = _record_context_evaluator()
    ev.evaluate_after_tool(
        tool_name="search",
        tool_result="some-data",
        agent_name="a",
        runtime_id="r",
    )
    ctx = captured["ctx"]
    assert ctx.hook == LifecycleHook.AFTER_TOOL
    assert ctx.tool_name == "search"
    assert ctx.tool_result == "some-data"


# ---------------------------------------------------------------------------
# DISABLED mode — evaluate() short-circuits without emitting audit
# ---------------------------------------------------------------------------


def test_disabled_mode_returns_empty_audit_record() -> None:
    """DISABLED mode short-circuits the rule loop and audit emission."""
    rule = _rule_with_condition("contains", "model_output", "anything")
    pack = PolicyPack(name="p", version="1", description="", rules=[rule])
    idx = PolicyIndex()
    idx.add_pack(pack)
    ev = GovernanceEvaluator(
        policy_index=idx, enforcement_mode=EnforcementMode.DISABLED
    )

    audit = ev.evaluate(_ctx(model_output="contains anything"))
    assert audit.final_action == Action.ALLOW
    assert audit.evaluations == []
