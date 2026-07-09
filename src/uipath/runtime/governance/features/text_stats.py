"""Text statistics features: word_count, char_count, shannon_entropy."""
from __future__ import annotations

import math
from collections import Counter

from uipath.runtime.governance.native.models import CheckContext

from ._registry import register
from ._text import primary_text


@register("word_count")
def word_count(context: CheckContext) -> int:
    """Number of whitespace-separated tokens in the primary text field."""
    return len(primary_text(context).split())


@register("char_count")
def char_count(context: CheckContext) -> int:
    """Character count of the primary text field."""
    return len(primary_text(context))


@register("shannon_entropy")
def shannon_entropy(context: CheckContext) -> float:
    """Shannon entropy (bits/symbol) over the primary text field.

    English prose is typically 3.5–4.5 bits/char. Binary noise approaches 8.
    Returns 0.0 for empty input.
    """
    text = primary_text(context)
    if not text:
        return 0.0
    counts = Counter(text)
    total = len(text)
    return -sum((c / total) * math.log2(c / total) for c in counts.values())
