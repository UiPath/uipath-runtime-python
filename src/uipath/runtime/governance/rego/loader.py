"""Rego bundle loader: async bootstrap builder."""
from __future__ import annotations

import logging

from uipath.core.governance.models import LifecycleHook

from uipath.runtime.governance.rego.bundle_cache import (
    get_cache_dir,
    get_cached_bundle_path,
    get_cached_etag,
    save_bundle,
)

logger = logging.getLogger(__name__)

# Hook type string → LifecycleHook mapping
_HOOK_MAP: dict[str, LifecycleHook] = {
    "before_agent": LifecycleHook.BEFORE_AGENT,
    "after_agent": LifecycleHook.AFTER_AGENT,
    "before_model": LifecycleHook.BEFORE_MODEL,
    "after_model": LifecycleHook.AFTER_MODEL,
    "tool_call": LifecycleHook.TOOL_CALL,
    "after_tool": LifecycleHook.AFTER_TOOL,
}


async def build_rego_evaluator_async(service: object) -> object:
    """Fetch bundles via platform service and build a RegoEvaluator.

    Drop-in counterpart to the native evaluator's inline bootstrap:
    ``get_policy_async`` → ``build_policy_index_from_yaml`` → ``GovernanceEvaluator``.
    Takes any object that exposes ``retrieve_all_policies_async()`` and
    ``download_bundle_async(url)`` — typically ``UiPath().governance``.

    Returns a :class:`~uipath.runtime.governance.rego.evaluator.RegoEvaluator`
    on success, ``None`` when no bundles are available. Never raises.
    """
    try:
        return await _build_rego_evaluator_async_inner(service)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Rego evaluator build failed: %s", exc)
        return None


async def _build_rego_evaluator_async_inner(service: object) -> object:
    from pathlib import Path

    from uipath.runtime.governance.rego.evaluator import (
        RegoEvaluator,
        _extract_data_json_from_bundle,
    )
    from uipath.runtime.governance.native.backend_client import (
        resolve_organization_id,
        resolve_tenant_id,
    )

    org_id = resolve_organization_id()
    tenant_id = resolve_tenant_id()
    if not org_id or not tenant_id:
        logger.warning(
            "Rego build skipped: org_id or tenant_id unavailable; "
            "agent will run without custom Rego evaluation."
        )
        return None

    response = await service.retrieve_all_policies_async()  # type: ignore[union-attr]
    if not response.hook_bundles:
        return None

    cache_dir = get_cache_dir(org_id, tenant_id)
    hook_wasm_paths: dict[LifecycleHook, Path] = {}
    hook_data: dict[LifecycleHook, dict] = {}

    for bundle in response.hook_bundles:
        hook_type = bundle.hook_type
        cached_etag = get_cached_etag(cache_dir, hook_type)
        cached_path = get_cached_bundle_path(cache_dir, hook_type)

        if cached_etag != bundle.etag or cached_path is None:
            raw = await service.download_bundle_async(bundle.bundle_url)  # type: ignore[union-attr]
            save_bundle(cache_dir, hook_type, raw, bundle.etag)

        path = get_cached_bundle_path(cache_dir, hook_type)
        if path is None:
            logger.warning("Rego bundle unavailable for hook=%s after download", hook_type)
            continue

        lifecycle_hook = _HOOK_MAP.get(hook_type)
        if lifecycle_hook is None:
            logger.warning("Rego build: unknown hook type %r — skipping", hook_type)
            continue

        hook_wasm_paths[lifecycle_hook] = path
        data_json = _extract_data_json_from_bundle(path)
        if data_json is not None:
            hook_data[lifecycle_hook] = data_json

    if not hook_wasm_paths:
        logger.warning(
            "Rego build: no WASM bundles available; "
            "agent will run without custom Rego evaluation."
        )
        return None

    logger.info("Rego evaluator built (hooks=%s)", [h.value for h in hook_wasm_paths])
    return RegoEvaluator(hook_wasm_paths, hook_data=hook_data or None)
