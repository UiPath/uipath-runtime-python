"""Pre-computed feature signals for the Rego WASM evaluator.

Each feature module registers its functions via ``_registry.register()``.
Importing this package imports all feature modules, triggering registration.

Usage::

    from uipath.runtime.governance.features import compute_features, FEATURE_NAMES
"""
from __future__ import annotations

# Importing each module triggers its @register() calls.
from . import commitment, encoding, incident, sentiment, text_stats  # noqa: F401
from ._registry import _REGISTRY, compute_features

FEATURE_NAMES: frozenset[str] = frozenset(_REGISTRY)

__all__ = ["compute_features", "FEATURE_NAMES"]
