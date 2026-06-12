"""Tests for ``_extract_governable_text`` content extraction.

Replaces the old ``str(value)[:2000]`` path in ``_check_before_agent``
and ``_check_after_agent``. Pulls clean text out of structured shapes
(dicts, list-of-blocks, pydantic models) instead of letting dict-repr
noise leak into the regex-scanned blob.
"""

from __future__ import annotations

from dataclasses import dataclass

from uipath.runtime.governance.wrapper import (
    _GOVERNANCE_TEXT_CAP,
    _extract_governable_text,
)


def test_plain_string_passes_through() -> None:
    assert _extract_governable_text("hello world") == "hello world"


def test_none_returns_empty() -> None:
    assert _extract_governable_text(None) == ""


def test_dict_with_content_key_extracts_content_first() -> None:
    """The classic coded-agent output shape — content comes through clean."""
    out = _extract_governable_text(
        {"content": "Estimated cost: $780", "_meta": {"id": "abc"}}
    )
    assert out.startswith("Estimated cost: $780")
    # No dict-syntax noise — the prior str(...) path produced ``{'content': '...'}``.
    assert "{'content'" not in out
    assert "'_meta'" not in out


def test_dict_priority_keys_lead() -> None:
    """``content`` / ``text`` / etc. lead before remaining keys."""
    out = _extract_governable_text(
        {"trailing_meta": "noise-meta", "content": "primary-text"}
    )
    assert out.index("primary-text") < out.index("noise-meta")


def test_list_of_text_blocks_concatenates() -> None:
    """Anthropic-style content blocks."""
    out = _extract_governable_text(
        [
            {"type": "text", "text": "first part"},
            {"type": "image", "source": {"data": "..."}},
            {"type": "text", "text": "second part"},
        ]
    )
    assert "first part" in out
    assert "second part" in out


def test_openai_function_call_shape_extracts_arguments() -> None:
    """``arguments`` field on OpenAI-style function-call blocks."""
    out = _extract_governable_text(
        [
            {
                "type": "function_call",
                "name": "end_execution",
                "arguments": '{"content":"Cost: $1,200"}',
                "id": "fc_abc",
            }
        ]
    )
    assert "Cost: $1,200" in out


def test_numeric_scalars_are_skipped() -> None:
    """Numbers / booleans aren't governance text — they shouldn't pad the blob."""
    out = _extract_governable_text(
        {"content": "hello", "count": 42, "ok": True, "rate": 3.14}
    )
    assert out == "hello"


def test_pydantic_like_model_dump_is_walked() -> None:
    """Anything with ``model_dump()`` is walked as its dict form."""

    class Stub:
        def model_dump(self) -> dict:
            return {"content": "from pydantic"}

    assert _extract_governable_text(Stub()) == "from pydantic"


def test_dataclass_via_dict_method() -> None:
    """Objects exposing a ``dict()`` callable also walk via that path."""

    class Stub:
        def dict(self) -> dict:
            return {"content": "from dict"}

    assert _extract_governable_text(Stub()) == "from dict"


def test_plain_object_attribute_fallback() -> None:
    """Public attributes on opaque objects feed the walker."""

    @dataclass
    class Result:
        content: str
        _private: str = "ignored"

    out = _extract_governable_text(Result(content="visible"))
    assert "visible" in out
    assert "ignored" not in out


def test_cycle_in_structure_does_not_recurse_forever() -> None:
    a: dict = {"content": "outer"}
    b: dict = {"loop": a}
    a["loop"] = b
    # Should return without recursing infinitely.
    out = _extract_governable_text(a)
    assert "outer" in out


def test_text_is_capped_at_budget() -> None:
    """Long content is truncated so a runaway payload can't dominate scans."""
    big = "x" * (_GOVERNANCE_TEXT_CAP + 1000)
    out = _extract_governable_text(big)
    assert len(out) == _GOVERNANCE_TEXT_CAP


def test_nested_dict_content_extracted() -> None:
    """LangGraph-style state with messages nested under a key."""
    out = _extract_governable_text(
        {
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "Cost: $50"},
            ]
        }
    )
    assert "Cost: $50" in out


def test_unknown_block_type_with_no_text_returns_empty() -> None:
    """Image-only block with no text payload contributes nothing."""
    out = _extract_governable_text(
        [{"type": "image", "source": {"type": "base64", "data": "..."}}]
    )
    # Could be empty or contain just the base64 data — but should NOT
    # contain Python dict syntax characters that the old path emitted.
    assert "{'type'" not in out


# ---------------------------------------------------------------------------
# Budget — 64K is the current cap (raised from 8K to fit multi-turn chat).
# ---------------------------------------------------------------------------


def test_budget_cap_is_64k() -> None:
    """Documents the cap so a future drop won't go unnoticed."""
    assert _GOVERNANCE_TEXT_CAP == 64000


# ---------------------------------------------------------------------------
# Reverse list iteration — latest entry gets the budget first.
# ---------------------------------------------------------------------------


def test_lists_are_walked_in_reverse() -> None:
    """Latest list entry leads the extracted blob.

    Critical for chat history: the new user message lives at the end of
    the messages list and must be visible even when prior turns would
    otherwise fill the budget first.
    """
    out = _extract_governable_text(
        [{"text": "earliest"}, {"text": "middle"}, {"text": "latest"}]
    )
    assert out.index("latest") < out.index("middle") < out.index("earliest")


def test_long_chat_history_keeps_latest_user_message() -> None:
    """A long history must not push the latest message out of the budget.

    Regression for the prior 8K-cap + forward-walk combination, which
    silently dropped the latest user message once the conversation
    grew past ~7,800 chars of prior content.
    """
    bulky_prior = "x" * 2000
    messages = [{"role": "user", "content": bulky_prior}] * 40  # ~80K chars
    messages.append({"role": "user", "content": "Cost: $1,200 — latest"})

    out = _extract_governable_text({"messages": messages})
    assert "Cost: $1,200 — latest" in out


# ---------------------------------------------------------------------------
# latest_only — BEFORE_AGENT in a conversational agent
# ---------------------------------------------------------------------------


def test_latest_only_extracts_just_the_last_list_item() -> None:
    """``latest_only=True`` drops every list entry but the last one."""
    out = _extract_governable_text(
        {
            "messages": [
                {"role": "user", "content": "old message"},
                {"role": "assistant", "content": "old response"},
                {"role": "user", "content": "Cost: $1,200"},
            ]
        },
        latest_only=True,
    )
    assert "Cost: $1,200" in out
    assert "old message" not in out
    assert "old response" not in out


def test_latest_only_resets_inside_chosen_item() -> None:
    """Multi-block content inside the latest message is still walked fully.

    ``latest_only`` reduces the OUTER list (chat history) to its last
    entry, but multi-block content (text + tool_call + thinking)
    inside that latest message must still be extracted in full —
    otherwise we'd lose answer text that arrives in a non-final block.
    """
    out = _extract_governable_text(
        {
            "messages": [
                {"role": "user", "content": "old"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "part A"},
                        {
                            "type": "function_call",
                            "arguments": '{"answer":"part B"}',
                        },
                    ],
                },
            ]
        },
        latest_only=True,
    )
    assert "part A" in out
    assert "part B" in out
    assert "old" not in out


def test_latest_only_top_level_list() -> None:
    """``latest_only`` applies when the input itself is a list."""
    out = _extract_governable_text(
        [
            {"content": "history item 1"},
            {"content": "history item 2"},
            {"content": "latest input"},
        ],
        latest_only=True,
    )
    assert "latest input" in out
    assert "history item 1" not in out
    assert "history item 2" not in out


def test_latest_only_default_false_still_walks_all() -> None:
    """Default behavior unchanged — AFTER_AGENT etc. still see everything."""
    out = _extract_governable_text(
        {
            "messages": [
                {"role": "user", "content": "first"},
                {"role": "user", "content": "second"},
            ]
        }
    )
    assert "first" in out
    assert "second" in out


def test_latest_only_empty_list_is_empty() -> None:
    """Empty history → empty extraction."""
    assert _extract_governable_text({"messages": []}, latest_only=True) == ""


def test_messages_is_a_priority_content_key() -> None:
    """``messages`` (plural) leads ahead of non-priority keys.

    Without ``messages`` in the priority list, an input that also
    carries siblings like ``thread_id`` / ``metadata`` could siphon
    budget before the actual chat history is walked.
    """
    out = _extract_governable_text(
        {
            "thread_id": "abc-xyz",
            "metadata": {"foo": "bar"},
            "messages": [{"role": "user", "content": "primary content"}],
        }
    )
    assert "primary content" in out
    assert out.index("primary content") < (
        out.find("abc-xyz") if "abc-xyz" in out else len(out)
    )
