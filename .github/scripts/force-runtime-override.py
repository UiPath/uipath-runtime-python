#!/usr/bin/env python3
"""Force cross-repo uv resolves to use the runtime wheel under test."""

from __future__ import annotations

import re
import sys
from pathlib import Path


def _quoted(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _add_override(pyproject_path: Path, override: str) -> None:
    text = pyproject_path.read_text()
    quoted_override = _quoted(override)

    if override in text:
        return

    tool_uv_match = re.search(
        r"(?ms)^\[tool\.uv\]\n(?P<body>.*?)(?=^\[|\Z)",
        text,
    )
    if not tool_uv_match:
        text = (
            text.rstrip()
            + f"\n\n[tool.uv]\noverride-dependencies = [{quoted_override}]\n"
        )
        pyproject_path.write_text(text)
        return

    body = tool_uv_match.group("body")
    override_match = re.search(
        r"(?ms)^override-dependencies\s*=\s*\[(?P<items>.*?)\]\s*",
        body,
    )
    if override_match:
        items = override_match.group("items").strip()
        if quoted_override in items:
            return

        if items:
            replacement = f"override-dependencies = [{items}, {quoted_override}]\n"
        else:
            replacement = f"override-dependencies = [{quoted_override}]\n"

        body = (
            body[: override_match.start()] + replacement + body[override_match.end() :]
        )
    else:
        body = f"override-dependencies = [{quoted_override}]\n" + body

    text = (
        text[: tool_uv_match.start("body")] + body + text[tool_uv_match.end("body") :]
    )
    pyproject_path.write_text(text)


def main() -> None:
    """Add a local runtime wheel override to a pyproject and print the wheel path."""
    if len(sys.argv) != 3:
        print(
            "usage: force-runtime-override.py <pyproject.toml> <runtime-wheel-dir>",
            file=sys.stderr,
        )
        raise SystemExit(2)

    pyproject_path = Path(sys.argv[1])
    wheel_dir = Path(sys.argv[2])
    wheels = sorted(wheel_dir.glob("uipath_runtime-*.whl"))
    if not wheels:
        print(f"no uipath-runtime wheel found in {wheel_dir}", file=sys.stderr)
        raise SystemExit(1)
    if len(wheels) > 1:
        wheel_names = ", ".join(str(wheel) for wheel in wheels)
        print(
            f"expected one uipath-runtime wheel in {wheel_dir}, found: {wheel_names}",
            file=sys.stderr,
        )
        raise SystemExit(1)

    wheel = wheels[0].resolve()

    _add_override(pyproject_path, f"uipath-runtime @ {wheel.as_uri()}")
    print(wheel)


if __name__ == "__main__":
    main()
