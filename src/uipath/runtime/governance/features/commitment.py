"""Commitment language features: commitment_verb, commitment_amount, commitment_deadline."""
from __future__ import annotations

import re

from uipath.runtime.governance.native.models import CheckContext

from ._registry import register
from ._text import primary_text

_COMMITMENT_VERB_PATTERN = re.compile(
    r"(?i)("
    r"\brefund\b|\breimburse\b|"
    r"\bwarranty\b|\bwarrant(?:y|ed|ies)\b|\bguarante[ed]+\b|"
    r"\bsla\b|"
    r"\bwaive[d]?\b|"
    r"\b(?:we|i)\s+(?:will|shall|promise|commit|guarantee)\b|"
    r"\b(?:we|i|i'?ll)\s+(?:deliver|provide|complete|finish|"
    r"handover|hand\s+over|ship)\b|"
    r"\bfixed\s+(?:price|cost|fee|scope|bid|rate)\b|"
    r"\bcost\s*:\s*\$?\d|"
    r"\bquote\s*:\s*\$?\d|"
    r"\bdeliverables?\b|"
    r"\btimeline\s*:\s*\d+\s*(?:second|minute|hour|day|week|month|year)s?\b|"
    r"\bI\s+propose\b"
    r")"
)
_COMMITMENT_AMOUNT_FALLBACK = re.compile(
    r"(?:\$|€|£|¥|₹|USD|EUR|GBP|JPY|INR)\s*\d[\d,]*(?:\.\d+)?"
    r"|\b\d[\d,]*(?:\.\d+)?\s*(?:USD|EUR|GBP|JPY|INR|"
    r"dollars?|euros?|pounds?|yen|rupees?)\b"
)
_COMMITMENT_DEADLINE_PATTERN = re.compile(
    r"(?i)\bwithin\s+\d+\s*(?:second|minute|hour|day|week|month|year)s?\b"
    r"|\bby\s+(?:tomorrow|next\s+\w+|\d+/\d+(?:/\d+)?)\b"
)


@register("commitment_verb")
def commitment_verb(context: CheckContext) -> bool:
    """True if the primary text contains a commitment verb or proposal marker."""
    text = primary_text(context)
    if not text:
        return False
    return bool(_COMMITMENT_VERB_PATTERN.search(text))


@register("commitment_amount")
def commitment_amount(context: CheckContext) -> bool:
    """True if the primary text contains a currency-anchored monetary amount."""
    text = primary_text(context)
    if not text:
        return False
    return bool(_COMMITMENT_AMOUNT_FALLBACK.search(text))


@register("commitment_deadline")
def commitment_deadline(context: CheckContext) -> bool:
    """True if the primary text contains a deadline phrase."""
    text = primary_text(context)
    if not text:
        return False
    return bool(_COMMITMENT_DEADLINE_PATTERN.search(text))
