"""Tests for governance feature functions: encoding, sentiment, text_stats, incident, commitment, text, registry."""
from __future__ import annotations

from unittest.mock import MagicMock

from uipath.runtime.governance.native.models import CheckContext


def _ctx(**kwargs: str) -> CheckContext:
    from uipath.core.governance.models import LifecycleHook
    defaults: dict = {
        "hook": LifecycleHook.BEFORE_MODEL,
        "agent_name": "test-agent",
        "runtime_id": "test-run",
    }
    defaults.update(kwargs)
    return CheckContext(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _text.py — primary_text and messages_text
# ---------------------------------------------------------------------------

class TestPrimaryText:
    def test_returns_model_output_first(self) -> None:
        from uipath.runtime.governance.features._text import primary_text
        ctx = _ctx(model_output="hello", model_input="world")
        assert primary_text(ctx) == "hello"

    def test_falls_back_to_model_input(self) -> None:
        from uipath.runtime.governance.features._text import primary_text
        ctx = _ctx(model_input="fallback")
        assert primary_text(ctx) == "fallback"

    def test_falls_back_to_agent_output(self) -> None:
        from uipath.runtime.governance.features._text import primary_text
        ctx = _ctx(agent_output="agent-out")
        assert primary_text(ctx) == "agent-out"

    def test_falls_back_to_agent_input(self) -> None:
        from uipath.runtime.governance.features._text import primary_text
        ctx = _ctx(agent_input="agent-in")
        assert primary_text(ctx) == "agent-in"

    def test_falls_back_to_tool_result(self) -> None:
        from uipath.runtime.governance.features._text import primary_text
        ctx = _ctx(tool_result="tool-res")
        assert primary_text(ctx) == "tool-res"

    def test_returns_empty_when_all_empty(self) -> None:
        from uipath.runtime.governance.features._text import primary_text
        ctx = _ctx()
        assert primary_text(ctx) == ""

    def test_skips_whitespace_only_fields(self) -> None:
        from uipath.runtime.governance.features._text import primary_text
        ctx = _ctx(model_output="   ", model_input="text")
        assert primary_text(ctx) == "text"


class TestMessagesText:
    def test_empty_messages_returns_empty(self) -> None:
        from uipath.runtime.governance.features._text import messages_text
        ctx = _ctx()
        assert messages_text(ctx) == ""

    def test_string_content(self) -> None:
        from uipath.runtime.governance.features._text import messages_text
        ctx = _ctx()
        ctx.messages = [{"content": "hello"}, {"content": "world"}]
        result = messages_text(ctx)
        assert "hello" in result
        assert "world" in result

    def test_list_content_extracts_text_blocks(self) -> None:
        from uipath.runtime.governance.features._text import messages_text
        ctx = _ctx()
        ctx.messages = [{"content": [{"type": "text", "text": "block text"}]}]
        result = messages_text(ctx)
        assert "block text" in result

    def test_list_content_skips_non_text_blocks(self) -> None:
        from uipath.runtime.governance.features._text import messages_text
        ctx = _ctx()
        ctx.messages = [{"content": [{"type": "image", "url": "x"}]}]
        assert messages_text(ctx) == ""


# ---------------------------------------------------------------------------
# _registry.py — compute_features
# ---------------------------------------------------------------------------

class TestComputeFeatures:
    def test_empty_plan_returns_empty(self) -> None:
        from uipath.runtime.governance.features._registry import compute_features
        ctx = _ctx()
        assert compute_features(ctx, []) == {}

    def test_none_plan_returns_empty(self) -> None:
        from uipath.runtime.governance.features._registry import compute_features
        ctx = _ctx()
        assert compute_features(ctx, None) == {}

    def test_unknown_feature_skipped(self) -> None:
        from uipath.runtime.governance.features._registry import compute_features
        ctx = _ctx()
        result = compute_features(ctx, ["nonexistent_feature_xyz"])
        assert result == {}

    def test_exception_in_feature_skipped(self) -> None:
        from uipath.runtime.governance.features._registry import (
            _REGISTRY,
            compute_features,
        )
        _REGISTRY["_test_raise"] = lambda ctx: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            result = compute_features(_ctx(), ["_test_raise"])
            assert "_test_raise" not in result
        finally:
            _REGISTRY.pop("_test_raise", None)

    def test_known_feature_computed(self) -> None:
        from uipath.runtime.governance.features._registry import (
            _REGISTRY,
            compute_features,
        )
        _REGISTRY["_test_ok"] = lambda ctx: 42
        try:
            result = compute_features(_ctx(), ["_test_ok"])
            assert result == {"_test_ok": 42}
        finally:
            _REGISTRY.pop("_test_ok", None)


# ---------------------------------------------------------------------------
# encoding.py
# ---------------------------------------------------------------------------

class TestEncodingFeatures:
    def test_encoding_concern_events_zero_for_clean_text(self) -> None:
        from uipath.runtime.governance.features.encoding import encoding_concern_events
        ctx = _ctx(model_output="Hello, world!")
        assert encoding_concern_events(ctx) == 0

    def test_encoding_concern_events_detects_replacement_char(self) -> None:
        from uipath.runtime.governance.features.encoding import encoding_concern_events
        ctx = _ctx(model_output="Hello � world")
        assert encoding_concern_events(ctx) > 0

    def test_encoding_concern_events_empty_context(self) -> None:
        from uipath.runtime.governance.features.encoding import encoding_concern_events
        ctx = _ctx()
        assert encoding_concern_events(ctx) == 0

    def test_encoding_concern_ratio_zero_for_clean_text(self) -> None:
        from uipath.runtime.governance.features.encoding import encoding_concern_ratio
        ctx = _ctx(model_output="Clean text here")
        assert encoding_concern_ratio(ctx) == 0.0

    def test_encoding_concern_ratio_empty_context(self) -> None:
        from uipath.runtime.governance.features.encoding import encoding_concern_ratio
        ctx = _ctx()
        assert encoding_concern_ratio(ctx) == 0.0

    def test_encoding_concern_ratio_detects_mojibake(self) -> None:
        from uipath.runtime.governance.features.encoding import encoding_concern_ratio
        ctx = _ctx(model_output="café becomes cafÃ©")
        assert encoding_concern_ratio(ctx) > 0.0

    def test_encoding_concern_events_detects_hex_escape(self) -> None:
        from uipath.runtime.governance.features.encoding import encoding_concern_events
        ctx = _ctx(model_output=r"text with \x41 escape")
        assert encoding_concern_events(ctx) > 0


# ---------------------------------------------------------------------------
# text_stats.py
# ---------------------------------------------------------------------------

class TestTextStatsFeatures:
    def test_word_count(self) -> None:
        from uipath.runtime.governance.features.text_stats import word_count
        ctx = _ctx(model_output="hello world foo")
        assert word_count(ctx) == 3

    def test_char_count(self) -> None:
        from uipath.runtime.governance.features.text_stats import char_count
        ctx = _ctx(model_output="hello")
        assert char_count(ctx) == 5

    def test_shannon_entropy_empty_returns_zero(self) -> None:
        from uipath.runtime.governance.features.text_stats import shannon_entropy
        ctx = _ctx()
        assert shannon_entropy(ctx) == 0.0

    def test_shannon_entropy_nonzero_for_text(self) -> None:
        from uipath.runtime.governance.features.text_stats import shannon_entropy
        ctx = _ctx(model_output="abcabc")
        assert shannon_entropy(ctx) > 0.0

    def test_word_count_empty(self) -> None:
        from uipath.runtime.governance.features.text_stats import word_count
        ctx = _ctx()
        assert word_count(ctx) == 0


# ---------------------------------------------------------------------------
# commitment.py
# ---------------------------------------------------------------------------

class TestCommitmentFeatures:
    def test_commitment_verb_true(self) -> None:
        from uipath.runtime.governance.features.commitment import commitment_verb
        ctx = _ctx(model_output="We will deliver the report by Friday.")
        assert commitment_verb(ctx) is True

    def test_commitment_verb_false_empty(self) -> None:
        from uipath.runtime.governance.features.commitment import commitment_verb
        ctx = _ctx()
        assert commitment_verb(ctx) is False

    def test_commitment_verb_false_no_match(self) -> None:
        from uipath.runtime.governance.features.commitment import commitment_verb
        ctx = _ctx(model_output="The weather is nice today.")
        assert commitment_verb(ctx) is False

    def test_commitment_amount_true(self) -> None:
        from uipath.runtime.governance.features.commitment import commitment_amount
        ctx = _ctx(model_output="The cost is $500 USD.")
        assert commitment_amount(ctx) is True

    def test_commitment_amount_false_empty(self) -> None:
        from uipath.runtime.governance.features.commitment import commitment_amount
        ctx = _ctx()
        assert commitment_amount(ctx) is False

    def test_commitment_amount_false_no_match(self) -> None:
        from uipath.runtime.governance.features.commitment import commitment_amount
        ctx = _ctx(model_output="Just some regular text.")
        assert commitment_amount(ctx) is False

    def test_commitment_deadline_true(self) -> None:
        from uipath.runtime.governance.features.commitment import commitment_deadline
        ctx = _ctx(model_output="We will finish within 7 days.")
        assert commitment_deadline(ctx) is True

    def test_commitment_deadline_false_empty(self) -> None:
        from uipath.runtime.governance.features.commitment import commitment_deadline
        ctx = _ctx()
        assert commitment_deadline(ctx) is False

    def test_commitment_deadline_false_no_match(self) -> None:
        from uipath.runtime.governance.features.commitment import commitment_deadline
        ctx = _ctx(model_output="No specific deadline here.")
        assert commitment_deadline(ctx) is False


# ---------------------------------------------------------------------------
# incident.py
# ---------------------------------------------------------------------------

class TestIncidentFeatures:
    def test_incident_categories_empty_context(self) -> None:
        from uipath.runtime.governance.features.incident import incident_categories
        ctx = _ctx()
        result = incident_categories(ctx)
        assert all(v is False for v in result.values())
        assert "safety_refusal" in result

    def test_incident_categories_detects_safety_refusal(self) -> None:
        from uipath.runtime.governance.features.incident import incident_categories
        ctx = _ctx(model_output="I cannot help with that request.")
        result = incident_categories(ctx)
        assert result["safety_refusal"] is True

    def test_incident_categories_detects_tool_failure(self) -> None:
        from uipath.runtime.governance.features.incident import incident_categories
        ctx = _ctx(model_output="500 internal server error occurred.")
        result = incident_categories(ctx)
        assert result["tool_failure"] is True

    def test_incident_categories_detects_quota_exceeded(self) -> None:
        from uipath.runtime.governance.features.incident import incident_categories
        ctx = _ctx(model_output="rate limit exceeded, try again later.")
        result = incident_categories(ctx)
        assert result["quota_exceeded"] is True

    def test_incident_categories_clean_text_all_false(self) -> None:
        from uipath.runtime.governance.features.incident import incident_categories
        ctx = _ctx(model_output="The sky is blue and birds are singing.")
        result = incident_categories(ctx)
        assert all(v is False for v in result.values())


# ---------------------------------------------------------------------------
# sentiment.py
# ---------------------------------------------------------------------------

class TestSentimentFeatures:
    def test_vader_compound_empty_returns_zero(self) -> None:
        from uipath.runtime.governance.features.sentiment import vader_compound
        ctx = _ctx()
        assert vader_compound(ctx) == 0.0

    def test_vader_compound_positive_text(self) -> None:
        from uipath.runtime.governance.features.sentiment import vader_compound
        ctx = _ctx(model_output="I love this! It's absolutely wonderful and great!")
        score = vader_compound(ctx)
        assert score > 0.0

    def test_vader_compound_negative_text(self) -> None:
        from uipath.runtime.governance.features.sentiment import vader_compound
        ctx = _ctx(model_output="This is terrible, awful, and completely horrible.")
        score = vader_compound(ctx)
        assert score < 0.0

    def test_vader_compound_exception_returns_zero(self) -> None:
        from uipath.runtime.governance.features import sentiment as sent_mod
        ctx = _ctx(model_output="some text")
        original = sent_mod._analyzer
        sent_mod._analyzer = MagicMock()
        sent_mod._analyzer.polarity_scores.side_effect = RuntimeError("boom")
        try:
            from uipath.runtime.governance.features.sentiment import vader_compound
            result = vader_compound(ctx)
            assert result == 0.0
        finally:
            sent_mod._analyzer = original
