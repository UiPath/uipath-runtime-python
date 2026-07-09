"""Encoding integrity features: encoding_concern_ratio, encoding_concern_events."""
from __future__ import annotations

import re

from uipath.runtime.governance.native.models import CheckContext

from ._registry import register
from ._text import primary_text

_MOJIBAKE_BIGRAMS: tuple[str, ...] = (
    "Ã©", "Ã¨", "Ã¢", "Ã ", "Ã¹", "Ã®", "Ã´", "Ã§",
    "Ã„", "Ã–", "Ãœ", "ÃŸ",
    "â€™", "â€œ", "â€\x9d", "â€“", "â€”", "â€¢",
    "Â£", "Â°", "Â§", "Â¶", "Â©", "Â®",
    "ï¿", "¿½", "ï»", "»¿",
)
_HEX_ESCAPE_PATTERN = re.compile(r"\\x[0-9a-fA-F]{2}")


def _corruption_counts(text: str) -> tuple[int, int]:
    """Return (events, weighted_chars) for encoding corruption in text."""
    replacement_chars = text.count("�")
    literal_ufffd = text.count("\\ufffd")
    hex_escapes = len(_HEX_ESCAPE_PATTERN.findall(text))
    mojibake = sum(text.count(b) for b in _MOJIBAKE_BIGRAMS)

    events = replacement_chars + literal_ufffd + hex_escapes + mojibake
    weighted = (
        replacement_chars
        + 6 * literal_ufffd
        + 4 * hex_escapes
        + 2 * mojibake
    )
    return events, weighted


@register("encoding_concern_events")
def encoding_concern_events(context: CheckContext) -> int:
    r"""Absolute count of encoding corruption events in the primary text field.

    Each U+FFFD, literal � escape, \xHH hex escape, or mojibake bigram
    counts as one event. A value >= 2 is a strong signal in production output.
    """
    text = primary_text(context)
    if not text:
        return 0
    events, _ = _corruption_counts(text)
    return events


@register("encoding_concern_ratio")
def encoding_concern_ratio(context: CheckContext) -> float:
    r"""Weighted corruption ratio (0.0–1.0) for the primary text field.

    Weights: U+FFFD=1, literal �=6, \xHH=4, mojibake bigram=2.
    Typical threshold for a policy deny rule: > 0.05.
    Returns 0.0 for empty input.
    """
    text = primary_text(context)
    if not text:
        return 0.0
    _, weighted = _corruption_counts(text)
    return weighted / max(len(text), 1)
