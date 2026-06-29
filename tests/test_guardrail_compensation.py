"""Tests for the synchronous GuardrailCompensator.

The runtime layer builds the wire payload and hands it to the
injected provider. The provider owns batching / async / fire-and-
forget. HTTP/auth/URL/header concerns — including ``trace_id``
resolution — live behind the
:class:`uipath.core.governance.GovernanceCompensationProvider`
protocol and are exercised in the concrete provider's own tests.

These tests cover:

- ``disabled_guardrails`` — distilling fired ``guardrail_fallback`` rules
  into per-rule wire metadata.
- ``GuardrailCompensator.submit`` — short-circuits on empty input,
  wire-model assembly, provider invocation, and fail-open behavior
  when the provider raises.
- ``close`` is a no-op (kept for API symmetry).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from uipath.core.governance import (
    FiredRule,
    GovernanceCompensationProvider,
    GovernRequest,
)

from uipath.runtime.governance.native.guardrail_compensation import (
    GuardrailCompensator,
    disabled_guardrails,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _provider() -> MagicMock:
    """Mock satisfying the GovernanceCompensationProvider protocol."""
    return MagicMock(spec=GovernanceCompensationProvider)


def _rules(
    *validators: str,
    rule_id: str = "R1",
    rule_name: str = "n",
    pack: str = "p",
) -> list[FiredRule]:
    """Build a list of FiredRule wire models — one per validator."""
    return [
        FiredRule(
            rule_id=rule_id,
            rule_name=rule_name,
            pack_name=pack,
            validator=v,
        )
        for v in validators
    ]


# ---------------------------------------------------------------------------
# disabled_guardrails
# ---------------------------------------------------------------------------


def test_disabled_guardrails_returns_fired_rule_for_matched_disabled_guardrail() -> None:
    cond = SimpleNamespace(
        operator="guardrail_fallback",
        value={
            "validator": "pii_detection",
            "mapped_to_uipath": True,
            "policy_enabled": False,
        },
    )
    rule = SimpleNamespace(checks=[SimpleNamespace(conditions=[cond])], pack_name="")
    audit = SimpleNamespace(
        evaluations=[
            SimpleNamespace(matched=True, rule_id="R1", rule_name="PII guardrail")
        ]
    )
    policy_index = SimpleNamespace(
        get_rule=lambda rid: rule if rid == "R1" else None
    )

    out = disabled_guardrails(audit, policy_index)

    assert len(out) == 1
    fr = out[0]
    assert isinstance(fr, FiredRule)
    assert fr.rule_id == "R1"
    assert fr.rule_name == "PII guardrail"
    assert fr.pack_name == ""
    assert fr.validator == "pii_detection"


def test_disabled_guardrails_skips_unmatched_evaluations() -> None:
    audit = SimpleNamespace(
        evaluations=[SimpleNamespace(matched=False, rule_id="R1", rule_name="x")]
    )
    policy_index = SimpleNamespace(get_rule=lambda rid: None)
    assert disabled_guardrails(audit, policy_index) == []


def test_disabled_guardrails_skips_non_guardrail_conditions() -> None:
    cond = SimpleNamespace(operator="regex", value="some-pattern")
    rule = SimpleNamespace(checks=[SimpleNamespace(conditions=[cond])])
    audit = SimpleNamespace(
        evaluations=[SimpleNamespace(matched=True, rule_id="R1", rule_name="x")]
    )
    policy_index = SimpleNamespace(get_rule=lambda rid: rule)
    assert disabled_guardrails(audit, policy_index) == []


def test_disabled_guardrails_skips_enabled_guardrails() -> None:
    """Mapped to UiPath AND enabled → no compensation needed."""
    cond = SimpleNamespace(
        operator="guardrail_fallback",
        value={
            "validator": "pii_detection",
            "mapped_to_uipath": True,
            "policy_enabled": True,
        },
    )
    rule = SimpleNamespace(checks=[SimpleNamespace(conditions=[cond])], pack_name="")
    audit = SimpleNamespace(
        evaluations=[SimpleNamespace(matched=True, rule_id="R1", rule_name="x")]
    )
    policy_index = SimpleNamespace(get_rule=lambda rid: rule)
    assert disabled_guardrails(audit, policy_index) == []


def test_disabled_guardrails_skips_unmapped_guardrails() -> None:
    """Not mapped to UiPath → server can't fall back; skip."""
    cond = SimpleNamespace(
        operator="guardrail_fallback",
        value={
            "validator": "pii_detection",
            "mapped_to_uipath": False,
            "policy_enabled": False,
        },
    )
    rule = SimpleNamespace(checks=[SimpleNamespace(conditions=[cond])], pack_name="")
    audit = SimpleNamespace(
        evaluations=[SimpleNamespace(matched=True, rule_id="R1", rule_name="x")]
    )
    policy_index = SimpleNamespace(get_rule=lambda rid: rule)
    assert disabled_guardrails(audit, policy_index) == []


# ---------------------------------------------------------------------------
# GuardrailCompensator.submit — short-circuits
# ---------------------------------------------------------------------------


def test_submit_empty_rules_short_circuits() -> None:
    """No rules → no provider call."""
    provider = _provider()
    compensator = GuardrailCompensator(provider)
    compensator.submit([], {}, "before_model", "ts", "a", "r")
    provider.compensate.assert_not_called()


def test_submit_no_validators_short_circuits() -> None:
    """Rules with empty validator strings → no call (nothing to dispatch)."""
    provider = _provider()
    compensator = GuardrailCompensator(provider)
    rules = [FiredRule(rule_id="R", rule_name="n", pack_name="p", validator="")]
    compensator.submit(rules, {}, "before_model", "ts", "a", "r")
    provider.compensate.assert_not_called()


# ---------------------------------------------------------------------------
# GuardrailCompensator.submit — wire-model assembly + provider invocation
# ---------------------------------------------------------------------------


def test_submit_invokes_provider_with_govern_request() -> None:
    """The provider receives a GovernRequest carrying every wire field.

    ``trace_id`` is left empty on the wire — the injected provider
    resolves it at HTTP-call time.
    """
    provider = _provider()
    compensator = GuardrailCompensator(provider)
    rules = _rules("pii_detection", "harmful_content")

    compensator.submit(
        rules,
        {"content": "x"},
        "before_model",
        "2026-06-06T00:00:00Z",
        "langchain",
        "patch-langchain",
    )

    provider.compensate.assert_called_once()
    (request,) = provider.compensate.call_args.args
    assert isinstance(request, GovernRequest)
    # distinct validators drive the guardrail API call
    assert request.validators == ["pii_detection", "harmful_content"]
    assert request.rules == rules
    assert request.data == {"content": "x"}
    assert request.hook == "before_model"
    # ``trace_id`` is not carried on the wire — the provider resolves at HTTP time.
    assert request.trace_id in (None, "")
    assert request.src_timestamp == "2026-06-06T00:00:00Z"
    assert request.agent_name == "langchain"
    assert request.runtime_id == "patch-langchain"
    # Job-context fields are left for the provider to auto-fill from env.
    assert request.folder_key is None
    assert request.job_key is None
    assert request.process_key is None
    assert request.reference_id is None
    assert request.agent_version is None


def test_submit_dedupes_validators() -> None:
    """Multiple rules with the same validator collapse on the wire."""
    provider = _provider()
    compensator = GuardrailCompensator(provider)
    rules = _rules("pii_detection") + _rules("pii_detection", rule_id="R2")

    compensator.submit(rules, {}, "before_model", "ts", "a", "r")

    (request,) = provider.compensate.call_args.args
    assert request.validators == ["pii_detection"]
    # Per-rule metadata is preserved (one record per rule even with shared validator).
    assert len(request.rules) == 2


def test_submit_swallows_provider_errors() -> None:
    """A provider exception must never propagate to the caller / agent."""
    provider = _provider()
    provider.compensate.side_effect = RuntimeError("network down")
    compensator = GuardrailCompensator(provider)

    # Must not raise.
    compensator.submit(_rules("x"), {}, "before_model", "ts", "a", "r")

    provider.compensate.assert_called_once()


def test_submit_recovers_after_provider_error() -> None:
    """A failed call doesn't poison the compensator — the next call still fires."""
    provider = _provider()
    provider.compensate.side_effect = [RuntimeError("transient"), None]
    compensator = GuardrailCompensator(provider)

    compensator.submit(_rules("x"), {}, "before_model", "ts", "a", "r")
    compensator.submit(_rules("x"), {}, "before_model", "ts", "a", "r")

    assert provider.compensate.call_count == 2


# ---------------------------------------------------------------------------
# close — no-op for API symmetry
# ---------------------------------------------------------------------------


def test_close_is_a_noop() -> None:
    """``close()`` holds no resources to release; calling it twice is safe."""
    c = GuardrailCompensator(_provider())
    c.close()
    c.close()  # must not raise
