"""Rego bundle loader: startup build, ETag-driven download, background refresh."""
from __future__ import annotations

import logging
import os
import threading
import time

from uipath.core.governance.models import LifecycleHook

from uipath.runtime.governance.native.backend_client import (
    BACKEND_REQUEST_TIMEOUT_SECONDS,
    resolve_organization_id,
    resolve_tenant_id,
)
from uipath.runtime.governance.rego.api_client import (
    download_bundle,
    fetch_all_policies,
)
from uipath.runtime.governance.rego.bundle_cache import (
    get_cache_dir,
    get_cached_bundle_path,
    get_cached_etag,
    save_bundle,
)

logger = logging.getLogger(__name__)

# Module-level state
_rego_evaluator = None  # RegoEvaluator | None
_rego_prefetch_event: threading.Event | None = None
_rego_prefetch_lock = threading.Lock()
_rego_refresh_started = False
_rego_refresh_lock = threading.Lock()

_DEFAULT_REFRESH_SECONDS = 30

# Hook type string → LifecycleHook mapping
_HOOK_MAP: dict[str, LifecycleHook] = {
    "before_agent": LifecycleHook.BEFORE_AGENT,
    "after_agent": LifecycleHook.AFTER_AGENT,
    "before_model": LifecycleHook.BEFORE_MODEL,
    "after_model": LifecycleHook.AFTER_MODEL,
    "tool_call": LifecycleHook.TOOL_CALL,
    "after_tool": LifecycleHook.AFTER_TOOL,
}


def prefetch_rego_bundles() -> None:
    """Kick off a background download of changed WASM bundles. Non-blocking. Idempotent."""
    global _rego_prefetch_event

    with _rego_prefetch_lock:
        if _rego_evaluator is not None:
            return
        if _rego_prefetch_event is not None:
            return
        event = threading.Event()
        _rego_prefetch_event = event

    def _worker() -> None:
        try:
            _sync_bundles_to_disk()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Rego bundle prefetch failed: %s", exc)
        finally:
            event.set()

    threading.Thread(
        target=_worker,
        name="governance-rego-prefetch",
        daemon=True,
    ).start()

    _start_background_refresh()


def _sync_bundles_to_disk() -> None:
    """Fetch /all-policies and download any bundle whose ETag has changed."""
    org_id = resolve_organization_id()
    tenant_id = resolve_tenant_id()
    if not org_id or not tenant_id:
        logger.warning(
            "Rego bundle sync skipped: org_id or tenant_id unavailable; "
            "agent will run without custom Rego policies."
        )
        return

    cache_dir = get_cache_dir(org_id, tenant_id)
    response = fetch_all_policies()
    if response is None:
        logger.warning(
            "Rego bundle sync: /all-policies fetch returned nothing; "
            "using cached bundles if available."
        )
        return

    for bundle in response.hook_bundles:
        hook_type = bundle.hook_type
        cached_etag = get_cached_etag(cache_dir, hook_type)

        if cached_etag == bundle.etag:
            logger.debug("Rego bundle for hook=%s is up to date (etag=%s)", hook_type, bundle.etag)
            continue

        logger.info(
            "Rego bundle for hook=%s changed (cached=%s new=%s); downloading",
            hook_type, cached_etag, bundle.etag,
        )
        data = download_bundle(bundle.bundle_url)
        if data is None:
            stale = get_cached_bundle_path(cache_dir, hook_type)
            if stale:
                logger.warning(
                    "Rego bundle download failed for hook=%s; using stale cache.", hook_type
                )
            else:
                logger.warning(
                    "Rego bundle download failed for hook=%s and no cache exists; "
                    "hook will pass through without Rego evaluation.",
                    hook_type,
                )
            continue

        save_bundle(cache_dir, hook_type, data, bundle.etag)
        logger.info("Rego bundle saved for hook=%s (etag=%s)", hook_type, bundle.etag)
        # Invalidate the in-memory evaluator so the next run picks up the new bundle.
        global _rego_evaluator
        _rego_evaluator = None


def get_rego_evaluator():
    """Return the cached RegoEvaluator, building it from disk if needed.

    Waits for the startup prefetch (bounded by BACKEND_REQUEST_TIMEOUT_SECONDS).
    Returns None when no bundles are available (fail-open).
    """
    global _rego_evaluator

    if _rego_evaluator is not None:
        return _rego_evaluator

    event = _rego_prefetch_event
    if event is not None:
        completed = event.wait(timeout=BACKEND_REQUEST_TIMEOUT_SECONDS)
        if not completed:
            logger.warning(
                "Rego bundle prefetch timed out after %.1fs; "
                "using whatever is on disk (may be empty).",
                BACKEND_REQUEST_TIMEOUT_SECONDS,
            )

    return _build_evaluator_from_disk()


def _build_evaluator_from_disk():
    """Read the disk cache and construct a RegoEvaluator. Returns None if no bundles exist."""
    from pathlib import Path

    from uipath.runtime.governance.rego.evaluator import (
        RegoEvaluator,
        _extract_data_json_from_bundle,
    )

    org_id = resolve_organization_id()
    tenant_id = resolve_tenant_id()
    if not org_id or not tenant_id:
        return None

    cache_dir = get_cache_dir(org_id, tenant_id)
    hook_wasm_paths: dict[LifecycleHook, Path] = {}
    hook_data: dict[LifecycleHook, dict] = {}

    for hook_type_str, lifecycle_hook in _HOOK_MAP.items():
        path = get_cached_bundle_path(cache_dir, hook_type_str)
        if path is not None:
            hook_wasm_paths[lifecycle_hook] = path
            data = _extract_data_json_from_bundle(path)
            if data is not None:
                hook_data[lifecycle_hook] = data

    if not hook_wasm_paths:
        logger.warning(
            "No Rego WASM bundles found on disk; "
            "agent will run without custom Rego evaluation."
        )
        return None

    logger.info(
        "Building RegoEvaluator from disk: hooks=%s",
        [h.value for h in hook_wasm_paths],
    )
    return RegoEvaluator(hook_wasm_paths, hook_data=hook_data or None)


def _start_background_refresh() -> None:
    """Start the background refresh daemon (once per process)."""
    global _rego_refresh_started

    refresh_seconds = int(
        os.environ.get("UIPATH_GOVERNANCE_BUNDLE_REFRESH_SECONDS", _DEFAULT_REFRESH_SECONDS)
    )
    if refresh_seconds <= 0:
        return

    with _rego_refresh_lock:
        if _rego_refresh_started:
            return
        _rego_refresh_started = True

    def _refresh_loop() -> None:
        while True:
            time.sleep(refresh_seconds)
            try:
                _sync_bundles_to_disk()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Rego bundle background refresh failed: %s", exc)

    threading.Thread(
        target=_refresh_loop,
        name="governance-rego-refresh",
        daemon=True,
    ).start()
    logger.debug("Rego bundle background refresh started (interval=%ds)", refresh_seconds)


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


def clear_rego_cache() -> None:
    """Reset all module-level state. Intended for tests."""
    global _rego_evaluator, _rego_prefetch_event, _rego_refresh_started
    with _rego_prefetch_lock:
        _rego_evaluator = None
        _rego_prefetch_event = None
    with _rego_refresh_lock:
        _rego_refresh_started = False
    logger.debug("Rego loader cache cleared")
