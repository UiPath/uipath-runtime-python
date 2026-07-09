"""Incident detection feature: incident_categories."""
from __future__ import annotations

import re

from uipath.runtime.governance.native.models import CheckContext

from ._registry import register
from ._text import primary_text

_INCIDENT_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "safety_refusal": [
        re.compile(
            r"(?i)\b(i\s+(?:cannot|can'?t|am\s+unable\s+to|won'?t\s+be\s+able\s+to)"
            r"\s+(?:help|assist|provide|answer|do\s+that))\b"
        ),
        re.compile(r"(?i)\b(i'?m\s+sorry,?\s+but\s+i\s+(?:cannot|can'?t))\b"),
        re.compile(r"(?i)\b(against\s+my\s+(?:guidelines|policies|programming))\b"),
    ],
    "tool_failure": [
        re.compile(
            r"\b(5\d{2})\b\s*(?:internal\s+server\s+error|service\s+unavailable)"
        ),
        re.compile(r"(?i)\b(ERR_[A-Z_]+|connection\s+refused|ECONNREFUSED)\b"),
        re.compile(r"(?i)\b(timed?\s*out|timeout)\b"),
    ],
    "auth_failure": [
        re.compile(r"\b(401|403)\b\s*(?:unauthori[sz]ed|forbidden)"),
        re.compile(
            r"(?i)\b(authentication\s+failed|invalid\s+(?:token|credentials))\b"
        ),
    ],
    "quota_exceeded": [
        re.compile(r"\b(429)\b"),
        re.compile(
            r"(?i)\b(rate\s+limit\s+exceeded|quota\s+exceeded|too\s+many\s+requests)\b"
        ),
    ],
    "hallucination": [
        re.compile(r"(?i)\b(i\s+(?:made\s+(?:that|this)\s+up|am\s+just\s+guessing))\b"),
        re.compile(r"(?i)\b(i\s+don'?t\s+actually\s+know|i\s+fabricat(?:ed|ing))\b"),
    ],
}


@register("incident_categories")
def incident_categories(context: CheckContext) -> dict[str, bool]:
    """Categorical incident detection over the primary text field.

    Returns a dict mapping each category name to True/False.
    Categories: safety_refusal, tool_failure, auth_failure, quota_exceeded, hallucination.
    """
    text = primary_text(context)
    result: dict[str, bool] = {}
    for category, patterns in _INCIDENT_PATTERNS.items():
        if not text:
            result[category] = False
        else:
            result[category] = any(p.search(text) for p in patterns)
    return result
