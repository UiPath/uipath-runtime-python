"""Governance policy API client.

Fetches the governance backend response so policies can be controlled
centrally without redeploying agents. Called once at process startup
from :mod:`uipath.runtime.governance.native.loader`; per-hook evaluation
stays in-process.

Response shape (JSON)::

    {
        "mode": "audit" | "enforce" | "disabled",
        "policies": "<YAML string>"
    }

``mode`` is the platform-controlled enforcement mode for the tenant;
the loader applies it via
:func:`uipath.runtime.governance.config.set_enforcement_mode`. ``policies``
is the YAML the evaluator compiles into a :class:`PolicyIndex`.

Failure mode is fail-open: when the organization id is unknown, the
access token is missing, the backend errors (one retry on transient
failures), or the body can't be parsed, the caller falls back to an
empty PolicyIndex. Nothing in this module ever raises to the caller.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlencode

from uipath.runtime.governance.native.backend_client import (
    AGENT_TYPE_PARAM,
    BACKEND_REQUEST_TIMEOUT_SECONDS,
    ENV_ACCESS_TOKEN,
    ENV_ORGANIZATION_ID,
    ENV_TENANT_ID,
    POLICY_API_PATH,
    TENANT_HEADER,
    agent_type_param,
    build_governance_url,
    governance_request_headers,
    resolve_organization_id,
    resolve_tenant_id,
)

logger = logging.getLogger(__name__)

# Re-exported alias kept for callers that imported the old name.
POLICY_API_TIMEOUT_SECONDS = BACKEND_REQUEST_TIMEOUT_SECONDS


@dataclass(frozen=True)
class PolicyResponse:
    """Parsed governance backend response.

    Attributes:
        mode: Enforcement mode string the backend returned
            (``"audit"`` / ``"enforce"`` / ``"disabled"``), or ``None``
            when the backend omitted it. Loader applies this via
            :func:`uipath.runtime.governance.config.set_enforcement_mode`.
        policy: Policy pack YAML to compile into a ``PolicyIndex``. May
            be an empty string if the backend returned no rules.
    """

    mode: str | None
    policy: str


def build_policy_url(org_id: str) -> str:
    """Build the policy endpoint URL for the given organization id.

    The tenant id is not part of the URL; it travels in the
    ``x-uipath-internal-tenantid`` request header (see
    :func:`fetch_policy_response`).

    When the hosted agent's type is known (see
    :func:`uipath.runtime.governance.native.backend_client.set_agent_conversational`),
    an ``agentType`` query param is appended so the server resolves the
    conversational-vs-autonomous container key. Omitted when unknown — the
    server then applies its default.
    """
    url = build_governance_url(org_id, POLICY_API_PATH)
    agent_type = agent_type_param()
    if agent_type:
        url = f"{url}?{urlencode({AGENT_TYPE_PARAM: agent_type})}"
    return url


def fetch_policy_response() -> PolicyResponse | None:
    """Fetch the governance backend's policy response.

    Single shot, no retry: a failed fetch (timeout / network error /
    HTTP error / malformed body) returns ``None`` and the caller falls
    back to an empty PolicyIndex. The agent must not spend time on a
    second attempt — keeping governance off the critical path is more
    important than maximising policy availability.

    Returns:
        :class:`PolicyResponse` on success. ``None`` on any failure
        path — caller falls back to an empty PolicyIndex.

    Never raises.
    """
    try:
        return _fetch_policy_response_inner()
    except Exception as exc:  # noqa: BLE001 - loader path must never raise
        logger.warning("Policy fetch failed unexpectedly: %s", exc)
        return None


def _fetch_policy_response_inner() -> PolicyResponse | None:
    org_id = resolve_organization_id()
    if not org_id:
        logger.warning(
            "Policy fetch skipped: UiPathConfig.organization_id is not "
            "available (set %s in the environment, or ensure uipath-platform "
            "is installed); governance will run with no policies. The "
            "backend API was NOT contacted.",
            ENV_ORGANIZATION_ID,
        )
        return None

    tenant_id = resolve_tenant_id()
    if not tenant_id:
        logger.warning(
            "Policy fetch skipped: UiPathConfig.tenant_id is not "
            "available (set %s in the environment, or ensure uipath-platform "
            "is installed); governance will run with no policies. The "
            "backend API was NOT contacted.",
            ENV_TENANT_ID,
        )
        return None

    policy_url = build_policy_url(org_id)

    token = os.environ.get(ENV_ACCESS_TOKEN)
    if not token:
        logger.warning(
            "Policy fetch skipped: %s is not set in the environment; "
            "governance will run with no policies.",
            ENV_ACCESS_TOKEN,
        )
        return None

    headers = governance_request_headers(json_body=True)
    headers[TENANT_HEADER] = tenant_id
    logger.info("Policy fetch starting (org=%s, tenant=%s)", org_id, tenant_id)

    body = _get_once(policy_url, headers)
    if body is None:
        return None
    return _parse_policy_body(body)


def _get_once(url: str, headers: dict[str, str]) -> bytes | None:
    """GET ``url`` once. Returns body bytes, or ``None`` on any failure.

    No retry by design — see :func:`fetch_policy_response` for the
    rationale. Every failure path logs a single WARNING and returns
    ``None`` so the caller (the loader) falls back to an empty
    PolicyIndex without delay.
    """
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(  # noqa: S310 - URL is built from config
            request, timeout=BACKEND_REQUEST_TIMEOUT_SECONDS
        ) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        logger.warning("Policy fetch returned HTTP %d: %s", exc.code, exc)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.warning("Policy fetch failed: %s", exc)
    return None


def _parse_policy_body(body: bytes) -> PolicyResponse | None:
    """Parse the JSON envelope into a :class:`PolicyResponse`."""
    if not body:
        logger.warning("Policy fetch returned empty body")
        return None

    try:
        payload = json.loads(body.decode("utf-8"))
    except UnicodeDecodeError as exc:
        logger.warning("Policy fetch returned non-UTF8 body: %s", exc)
        return None
    except json.JSONDecodeError as exc:
        logger.warning(
            "Policy fetch returned malformed JSON "
            "(server may have returned an HTML error page): %s",
            exc,
        )
        return None

    if not isinstance(payload, dict):
        logger.warning(
            "Policy fetch returned unexpected JSON shape (expected object, got %s)",
            type(payload).__name__,
        )
        return None

    raw_mode = payload.get("mode")
    mode = raw_mode if isinstance(raw_mode, str) and raw_mode else None

    raw_policy = payload.get("policies", "")
    if not isinstance(raw_policy, str):
        logger.warning(
            "Policy fetch returned non-string 'policies' field (got %s)",
            type(raw_policy).__name__,
        )
        return None

    logger.info(
        "Policy fetch ok: mode=%s, policy_bytes=%d", mode, len(raw_policy)
    )
    return PolicyResponse(mode=mode, policy=raw_policy)
