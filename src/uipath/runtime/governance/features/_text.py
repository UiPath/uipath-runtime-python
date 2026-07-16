"""Text extraction helpers for feature functions."""
from __future__ import annotations

from uipath.runtime.governance.native.models import CheckContext


def primary_text(context: CheckContext) -> str:
    """Return the most content-rich populated text field for the active hook."""
    for field in (
        context.model_output,
        context.model_input,
        context.agent_output,
        context.agent_input,
        context.tool_result,
    ):
        if field and isinstance(field, str) and field.strip():
            return field
    return ""


def messages_text(context: CheckContext) -> str:
    """Concatenate all message content blocks into one string."""
    if not context.messages:
        return ""
    parts: list[str] = []
    for msg in context.messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
    return " ".join(filter(None, parts))
