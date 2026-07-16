"""Sentiment feature: vader_compound."""
from __future__ import annotations

from typing import Any

from uipath.runtime.governance.native.models import CheckContext

from ._registry import register
from ._text import primary_text

_analyzer: Any = None


def _get_analyzer() -> Any:
    global _analyzer
    if _analyzer is None:
        from vaderSentiment.vaderSentiment import (  # type: ignore[import-untyped]
            SentimentIntensityAnalyzer,
        )
        _analyzer = SentimentIntensityAnalyzer()
    return _analyzer


@register("vader_compound")
def vader_compound(context: CheckContext) -> float:
    """VADER compound sentiment score (-1.0 to 1.0) of the primary text field.

    Returns 0.0 for empty input or when vaderSentiment is not installed.
    """
    text = primary_text(context)
    if not text:
        return 0.0
    try:
        return float(_get_analyzer().polarity_scores(text)["compound"])
    except Exception:  # noqa: BLE001
        return 0.0
