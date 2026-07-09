"""Governance Rego bundle API client.

Fetches per-hook WASM bundle metadata from ``GET /all-policies/:tenantId``
and downloads individual bundles from their CDN URLs.

Failure mode is fail-open throughout: every public function returns
``None`` / empty on any error so callers fall back to stale cache or
run without Rego evaluation.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

from uipath.runtime.governance.native.backend_client import (
    ALL_POLICIES_API_PATH,
    BACKEND_REQUEST_TIMEOUT_SECONDS,
    ENV_ACCESS_TOKEN,
    TENANT_HEADER,
    build_governance_url,
    governance_request_headers,
    resolve_organization_id,
    resolve_tenant_id,
)
from uipath.runtime.governance.rego.models import AllPoliciesResponse, HookBundle

logger = logging.getLogger(__name__)


def build_all_policies_url(org_id: str, tenant_id: str) -> str:
    """Build the ``/all-policies/:tenantId`` endpoint URL."""
    return build_governance_url(org_id, f"{ALL_POLICIES_API_PATH}/{tenant_id}")


def fetch_all_policies() -> AllPoliciesResponse | None:
    """Fetch per-hook bundle metadata from the governance backend.

    Returns AllPoliciesResponse on success, None on any failure. Never raises.
    """
    try:
        return _fetch_all_policies_inner()
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_all_policies failed unexpectedly: %s", exc)
        return None


def _fetch_all_policies_inner() -> AllPoliciesResponse | None:
    org_id = resolve_organization_id()
    if not org_id:
        logger.warning(
            "Rego bundle fetch skipped: organization_id unavailable; "
            "agent will run without custom Rego policies."
        )
        return None

    tenant_id = resolve_tenant_id()
    if not tenant_id:
        logger.warning(
            "Rego bundle fetch skipped: tenant_id unavailable; "
            "agent will run without custom Rego policies."
        )
        return None

    token = os.environ.get(ENV_ACCESS_TOKEN)
    if not token:
        logger.warning(
            "Rego bundle fetch skipped: %s not set; "
            "agent will run without custom Rego policies.",
            ENV_ACCESS_TOKEN,
        )
        return None

    url = build_all_policies_url(org_id, tenant_id)
    headers = governance_request_headers(json_body=False)
    headers[TENANT_HEADER] = tenant_id

    body = _get_once(url, headers)
    if body is None:
        return None
    return _parse_response(body)


def _get_once(url: str, headers: dict[str, str]) -> bytes | None:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(  # noqa: S310
            request, timeout=BACKEND_REQUEST_TIMEOUT_SECONDS
        ) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        logger.warning("Rego bundle fetch returned HTTP %d: %s", exc.code, exc)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.warning("Rego bundle fetch failed: %s", exc)
    return None


def _parse_response(body: bytes) -> AllPoliciesResponse | None:
    if not body:
        logger.warning("Rego bundle fetch: empty response body")
        return None
    try:
        payload = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning("Rego bundle fetch: malformed response: %s", exc)
        return None

    if not isinstance(payload, dict):
        logger.warning("Rego bundle fetch: unexpected JSON shape")
        return None

    raw_bundles = payload.get("hookBundles", [])
    if not isinstance(raw_bundles, list):
        logger.warning("Rego bundle fetch: hookBundles is not a list")
        return None

    bundles: list[HookBundle] = []
    for item in raw_bundles:
        if not isinstance(item, dict):
            continue
        hook_type = item.get("hookType", "")
        url_str = item.get("bundleUrl", "")
        etag = item.get("etag", "")
        if hook_type and url_str:
            bundles.append(HookBundle(hook_type=hook_type, bundle_url=url_str, etag=etag))

    return AllPoliciesResponse(hook_bundles=bundles)


def download_bundle(url: str) -> bytes | None:
    """Download a WASM bundle from a pre-signed CDN URL.

    No auth header is sent — the URL is pre-signed. Returns raw bytes or None. Never raises.
    """
    try:
        request = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(  # noqa: S310
            request, timeout=BACKEND_REQUEST_TIMEOUT_SECONDS
        ) as response:
            return response.read()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Bundle download from %s failed: %s", url, exc)
        return None
