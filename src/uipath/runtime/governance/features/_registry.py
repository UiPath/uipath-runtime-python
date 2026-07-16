"""Feature registry for Rego WASM pre-computed signals.

Feature modules call ``register("name")`` at module level.
``compute_features`` is the only function callers outside this package need.
"""
from __future__ import annotations

from typing import Any, Callable

from uipath.runtime.governance.native.models import CheckContext

_REGISTRY: dict[str, Callable[[CheckContext], Any]] = {}


def register(name: str) -> Callable[[Callable[[CheckContext], Any]], Callable[[CheckContext], Any]]:
    """Decorator that registers a feature function under *name*."""
    def decorator(fn: Callable[[CheckContext], Any]) -> Callable[[CheckContext], Any]:
        _REGISTRY[name] = fn
        return fn
    return decorator


def compute_features(context: CheckContext, plan: list[str] | None) -> dict[str, Any]:
    """Compute only the features listed in *plan*.

    Unknown names are silently skipped.
    Any exception during a feature function excludes that feature from the result.
    """
    if not plan:
        return {}
    result: dict[str, Any] = {}
    for name in plan:
        fn = _REGISTRY.get(name)
        if fn is None:
            continue
        try:
            result[name] = fn(context)
        except Exception:  # noqa: BLE001
            pass
    return result
