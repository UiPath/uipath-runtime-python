"""Tests for the commitment_concern check (A.10.4).

The check now uses OR semantics: a verb match, an amount match, or a
deadline match is each sufficient when its enabling flag is on. With
both flags false the rule matches verb-only.

The verb pattern also covers proposal / SOW style commitment markers
("Cost: $X", "fixed scope", "Deliverables", "Timeline", "I propose")
so formal-business commitments without first-person verbs still fire.

Amount detection requires a currency marker adjacent to the number to
prevent URL fragments (forum-post IDs, image dimensions, etc.) from
false-positiving.
"""

from __future__ import annotations

import pytest

from uipath.runtime.governance.native.evaluator import GovernanceEvaluator

# ---------------------------------------------------------------------------
# The proposal-style sample that originally slipped through the rule.
# Contains: "Cost: $780 (fixed for the above scope)", "Deliverables",
# "Timeline: 4 days total", "I propose", a forum URL with a 6-digit ID.
# Triple-quoted so we keep the line breaks the model produced.
# ---------------------------------------------------------------------------
SAMPLE_PROPOSAL = """To address your concerns, I reviewed the official UiPath site you referenced and relevant resources on uipath.com to inform a fast stabilization plan. Notable findings include: a community CI/CD sample for UiPath projects (https://forum.uipath.com/t/announcement-ci-cd-pipeline-sample-implementation-s-for-uipath-projects-alpha/667851).

Here's how I propose we turn your software around quickly:

Plan
- Triage (logs + reproduce)
- Quick stabilization

Deliverables
- Defect triage report

Timeline: 4 days total
- Day 1: Triage + reproduction

Cost: $780 (fixed for the above scope)
"""


@pytest.mark.parametrize(
    "text",
    [
        "Cost: $780 (fixed for the above scope)",
        "Deliverables: a, b, c",
        "Timeline: 4 days total for the whole engagement",
        "I propose we turn this around in a week",
        "We will refund the difference",
        "I'll deliver the report by Friday",
        "the warranty covers parts only",
        "fixed price of one hundred dollars",
    ],
)
def test_verb_match_alone_fires(text: str) -> None:
    """Each verb-style commitment marker fires on its own (verb-only mode)."""
    assert (
        GovernanceEvaluator._check_commitment_concern(
            text, {"require_amount": False, "require_deadline": False}
        )
        is True
    )


def test_full_proposal_sample_fires() -> None:
    """The originally-missed proposal output now fires."""
    assert (
        GovernanceEvaluator._check_commitment_concern(
            SAMPLE_PROPOSAL,
            {"require_amount": False, "require_deadline": False},
        )
        is True
    )


@pytest.mark.parametrize(
    "text",
    [
        "$780",
        "We charge USD 1,200 per seat",
        "The fee is 500 EUR",
    ],
)
def test_amount_alone_fires_when_require_amount_true(text: str) -> None:
    """Currency-anchored amount alone fires under OR semantics."""
    assert (
        GovernanceEvaluator._check_commitment_concern(
            text, {"require_amount": True, "require_deadline": False}
        )
        is True
    )


@pytest.mark.parametrize(
    "text",
    [
        "Task is 75% complete.",
        "We maintain 99.9% uptime.",
        "Battery at 50%.",
        "Score: 12%.",
    ],
)
def test_bare_percentage_does_not_fire(text: str) -> None:
    """Status-only percentages must not trigger commitment_concern.

    Regression for the prior ``\\d{1,3}\\s*%`` branch in the amount
    regex, which fired on benign status / progress text. Real
    percentage-bearing commitments ("we'll give a 20% discount")
    still fire via the verb pattern.
    """
    assert (
        GovernanceEvaluator._check_commitment_concern(
            text, {"require_amount": True, "require_deadline": False}
        )
        is False
    )


def test_percentage_with_verb_still_fires() -> None:
    """A commitment verb co-occurring with a percentage still fires."""
    assert (
        GovernanceEvaluator._check_commitment_concern(
            "We will refund 100% of the purchase price.",
            {"require_amount": True, "require_deadline": False},
        )
        is True
    )


def test_amount_alone_does_not_fire_when_require_amount_false() -> None:
    """Amount-only text is silent when require_amount=False and no verb."""
    assert (
        GovernanceEvaluator._check_commitment_concern(
            "The list price is $780.",
            {"require_amount": False, "require_deadline": False},
        )
        is False
    )


def test_deadline_alone_fires_when_require_deadline_true() -> None:
    """Deadline phrase alone fires under OR semantics."""
    assert (
        GovernanceEvaluator._check_commitment_concern(
            "Will be done within 5 days.",
            {"require_amount": False, "require_deadline": True},
        )
        is True
    )


def test_url_fragment_digits_do_not_false_positive() -> None:
    """A long URL with embedded digits is not a 'commitment'.

    Catches the prior price-parser misbehaviour where Price.fromstring()
    picked up forum-post IDs (e.g. ``667851``) and conflated them with
    unrelated currency symbols elsewhere in the text.
    """
    text = (
        "See https://forum.example.com/t/topic/667851 for details — "
        "no commitment language here."
    )
    assert (
        GovernanceEvaluator._check_commitment_concern(
            text, {"require_amount": True, "require_deadline": True}
        )
        is False
    )


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "Just chatting about the weather today.",
        "The product is durable and well-made.",
    ],
)
def test_no_signal_does_not_fire(text: str) -> None:
    """Text without any commitment signal stays silent regardless of flags."""
    assert (
        GovernanceEvaluator._check_commitment_concern(
            text, {"require_amount": True, "require_deadline": True}
        )
        is False
    )


def test_non_dict_params_treated_as_defaults() -> None:
    """``params`` of the wrong type degrades to defaults rather than crashing."""
    assert (
        GovernanceEvaluator._check_commitment_concern("we will refund", None)
        is True
    )
    assert (
        GovernanceEvaluator._check_commitment_concern(
            "no verbs here", "garbage"
        )
        is False
    )
