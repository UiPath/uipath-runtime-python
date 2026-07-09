"""Data models for the governance Rego/WASM bundle API response."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HookBundle:
    """Metadata for a single per-hook WASM bundle returned by /all-policies."""
    hook_type: str
    bundle_url: str
    etag: str


@dataclass(frozen=True)
class AllPoliciesResponse:
    """Parsed response from GET /all-policies/:tenantId."""
    hook_bundles: list[HookBundle]
