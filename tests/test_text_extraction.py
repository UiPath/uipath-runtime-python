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
