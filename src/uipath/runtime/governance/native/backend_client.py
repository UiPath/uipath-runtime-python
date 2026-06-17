"""Governance backend client.

Hosts the shared infrastructure used by every governance-backend call:

- :func:`get_backend_base_url` — resolves the cloud host (with the
  org/tenant path segments stripped) so each endpoint builder can
  append its own scoped path.
- :func:`governance_request_headers` — composes the headers shared by
  the policy fetch and the ``/runtime/govern`` compensating POST
  (Accept, User-Agent, optional Content-Type, optional Bearer auth).
- :func:`build_governance_url` — composes an org-scoped URL against
  the ``agenticgovernance_`` ingress.
- :func:`resolve_organization_id` / :func:`resolve_tenant_id` — read
  the active org/tenant from the environment (published by the UiPath
  runtime host), keeping runtime independent of ``uipath-platform``.
- :func:`safe_call` — fail-open helper that catches every non-block
  exception so governance hooks never crash an agent run.
- Module-level constants — request timeout, service path prefix,
  compensation pool size — all the tunables an operator might care
  about. Defined once here so the policy fetch, the compensating
  ``/runtime/govern`` call, and the loader share one definition.

The endpoint clients live next door:

- :mod:`uipath.runtime.governance.native.policy_api_client` — policy fetch
- :mod:`uipath.runtime.governance.native.guardrail_compensation` — /runtime/govern
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Callable
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------------
# Env-var names (consumed by the helpers below + diagnostic messages)
# ----------------------------------------------------------------------------

# Explicit dev/test override — used verbatim, no path-stripping.
ENV_BACKEND_BASE_URL = "UIPATH_GOVERNANCE_BACKEND_URL"
# The canonical platform URL env var.
ENV_PLATFORM_BASE_URL = "UIPATH_URL"
# Bearer token; missing means the policy fetch and compensating call are
# skipped (and that fact is logged) rather than producing 401s on every call.
ENV_ACCESS_TOKEN = "UIPATH_ACCESS_TOKEN"
# Org / tenant scoping for the agenticgovernance_ ingress.
ENV_ORGANIZATION_ID = "UIPATH_ORGANIZATION_ID"
ENV_TENANT_ID = "UIPATH_TENANT_ID"
# Trace id used to bind governance spans / compensation records to the
# agent's trace.
ENV_TRACE_ID = "UIPATH_TRACE_ID"
# Job-execution context forwarded in the /runtime/govern payload so the
# server can populate the LLMOps trace record (Doc-2 audit structure).
# Published into the process environment by the UiPath runtime host.
ENV_FOLDER_KEY = "UIPATH_FOLDER_KEY"
ENV_JOB_KEY = "UIPATH_JOB_KEY"
ENV_PROCESS_KEY = "UIPATH_PROCESS_UUID"
ENV_REFERENCE_ID = "UIPATH_AGENT_ID"
ENV_AGENT_VERSION = "UIPATH_PROCESS_VERSION"

# ----------------------------------------------------------------------------
# Endpoint shape — all governance calls hit the org-scoped agenticgovernance_
# service. Centralised so adding a third endpoint is "one new path constant"
# instead of "a new path template that someone forgets to keep in sync."
# ----------------------------------------------------------------------------

GOVERNANCE_SERVICE_PREFIX = "agenticgovernance_"
POLICY_API_PATH = "api/v1/runtime/policy"
GOVERN_API_PATH = "api/v1/runtime/govern"
TENANT_HEADER = "x-uipath-internal-tenantid"
# Query param on the policy fetch that selects the agent-type view of the
# policy: the server's clause-resolver reads the matching container key
# (``*-in-flight-conversational-agents`` vs ``*-in-flight-agents``). It's a
# representation selector (it changes the returned policy), so it travels as a
# query param — cache-correct and part of resource identification — not a
# header. Values: "conversational" | "autonomous".
AGENT_TYPE_PARAM = "agentType"
AGENT_TYPE_CONVERSATIONAL = "conversational"
AGENT_TYPE_AUTONOMOUS = "autonomous"

# Default base URL when no override and no UIPATH_URL value is
# available. Used only on developer machines doing fully-offline work; real
# deployments always have UIPATH_URL injected by the host.
_DEFAULT_BACKEND_BASE_URL = "https://alpha.uipath.com"

# ----------------------------------------------------------------------------
# Tunables — one place so an ops change is one edit. The values that bound
# how long a single agent run can spend on governance traffic.
# ----------------------------------------------------------------------------

# Per-request timeout for any governance backend HTTP call (policy fetch,
# /runtime/govern compensating POST). Same value used everywhere so an agent
# can't accidentally end up with a "long" timeout on one call and "short" on
# another.
BACKEND_REQUEST_TIMEOUT_SECONDS = 10.0

# Bound on concurrent /runtime/govern requests in flight. A misbehaving
# agent that fires `before_model` 100 times in a session with three matched
# fallback rules each would otherwise spawn 100 daemon threads; this pool
# caps the concurrency. Saturated submissions are logged and dropped — the
# server still receives traces from the requests that did land.
COMPENSATION_MAX_WORKERS = 4

# Browser-shaped User-Agent. Required because the alpha/production
# governance ingress runs a WAF whose default scanner rule set blocks
# ``Python-urllib/<version>``. Identifying as a real browser keeps the
# request from being rejected before any auth/tenant logic runs.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/148.0.0.0 Safari/537.36"
)


# ----------------------------------------------------------------------------
# Headers
# ----------------------------------------------------------------------------


def governance_request_headers(*, json_body: bool = False) -> dict[str, str]:
    """Return the common HTTP headers for governance backend requests.

    Centralises the headers shared between the policy fetch and the
    compensating ``/runtime/govern`` POST so the UA and auth shape are
    declared once.

    Args:
        json_body: When ``True`` (POST/PATCH/etc. with a JSON payload),
            adds ``Content-Type: application/json``. GETs leave it off
            so origin servers that 415 on unexpected Content-Type stay
            happy.

    Returns:
        A new dict with:

        - ``Accept: application/json``
        - ``User-Agent`` (the browser-shaped string above)
        - ``Content-Type: application/json`` when ``json_body=True``
        - ``Authorization: Bearer <UIPATH_ACCESS_TOKEN>`` when the env
          var is set; omitted otherwise (caller decides whether the
          missing token is fatal).

    Endpoint-specific headers (e.g. ``x-uipath-internal-tenantid``) are
    added by the caller after this helper returns.
    """
    headers: dict[str, str] = {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if json_body:
        headers["Content-Type"] = "application/json"
    token = os.environ.get(ENV_ACCESS_TOKEN)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


# ----------------------------------------------------------------------------
# URL composition
# ----------------------------------------------------------------------------


def _strip_to_origin(raw_url: str) -> str:
    """Return ``scheme://host[:port]`` for ``raw_url``, dropping any path.

    Platform URLs are commonly ``https://cloud.uipath.com/<org>/<tenant>``;
    the governance endpoints construct their own
    ``/{org}/agenticgovernance_/...`` suffix, so the org/tenant segments
    in the base must be stripped to avoid a duplicated org path.
    """
    parsed = urlparse(raw_url)
    if not parsed.scheme or not parsed.netloc:
        # Not a parseable absolute URL — leave it to the caller.
        return raw_url.rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}"


def get_backend_base_url() -> str:
    """Resolve the governance backend base URL on each call.

    Resolution order (first hit wins):

    1. ``UIPATH_GOVERNANCE_BACKEND_URL`` — explicit dev/test override,
       used verbatim.
    2. ``UIPATH_URL`` env var — the canonical platform URL. Org/tenant
       path segments are stripped so the caller can append its own
       org-scoped path.
    3. ``https://alpha.uipath.com`` — last-resort default for offline
       development; real deployments always have ``UIPATH_URL`` set.

    Reading on each call (not at import) lets the runtime entrypoint
    configure the env vars after this module is already loaded.
    """
    explicit_override = os.environ.get(ENV_BACKEND_BASE_URL)
    if explicit_override:
        return explicit_override.rstrip("/")

    raw = os.environ.get(ENV_PLATFORM_BASE_URL)
    if raw:
        return _strip_to_origin(raw)

    return _DEFAULT_BACKEND_BASE_URL


def build_governance_url(org_id: str, path: str) -> str:
    """Compose an org-scoped governance backend URL.

    Final shape: ``{backend_base}/{org_id}/{GOVERNANCE_SERVICE_PREFIX}/{path}``.

    Args:
        org_id: Active organization id; the URL is meaningless without it.
        path: API suffix WITHOUT the org/service prefix
            (e.g. :data:`POLICY_API_PATH` or :data:`GOVERN_API_PATH`).
    """
    base = get_backend_base_url()
    return f"{base}/{org_id}/{GOVERNANCE_SERVICE_PREFIX}/{path}"


# ----------------------------------------------------------------------------
# Org / tenant resolution
# ----------------------------------------------------------------------------


def _resolve_env_field(env_var: str) -> str | None:
    """Read a runtime-context value from its environment variable.

    Org/tenant ids and job context are published into the process
    environment by the UiPath runtime host. Reading them directly keeps
    ``uipath-runtime`` independent of ``uipath-platform`` (the lower layer
    must not import the higher one).
    """
    return os.environ.get(env_var)


# ----------------------------------------------------------------------------
# Agent-type selector (conversational vs autonomous)
#
# Set once by the governance wrapper at runtime init (before the background
# policy prefetch is kicked off) and read by the policy fetch when composing
# the request URL. A process-level holder — not a ContextVar — because the
# prefetch runs on a separate thread that wouldn't inherit a ContextVar, and a
# coded-agent process hosts a single agent so the value is stable per process.
# ----------------------------------------------------------------------------

_agent_is_conversational: bool | None = None


def set_agent_conversational(value: bool | None) -> None:
    """Record whether the hosted agent is conversational.

    ``None`` clears the selector (used by tests / direct callers); the policy
    fetch then omits the param and the server applies its default.
    """
    global _agent_is_conversational
    _agent_is_conversational = value


def agent_type_param() -> str | None:
    """Return the ``agentType`` query value, or ``None`` when unknown.

    ``"conversational"`` / ``"autonomous"`` map to the server's
    conversational-vs-autonomous container keys; ``None`` (selector never set)
    omits the param so the server's default applies.
    """
    if _agent_is_conversational is None:
        return None
    return AGENT_TYPE_CONVERSATIONAL if _agent_is_conversational else AGENT_TYPE_AUTONOMOUS


def resolve_organization_id() -> str | None:
    """Return the current organization id from the environment.

    Returns ``None`` when unset — callers skip the backend interaction
    (no URL can be built without an org id) and the agent runs with no
    policies / no compensation.
    """
    return _resolve_env_field(ENV_ORGANIZATION_ID)


def resolve_tenant_id() -> str | None:
    """Return the current tenant id from the environment.

    Returns ``None`` when unset — callers skip the backend interaction
    since the ``x-uipath-internal-tenantid`` header would be missing.
    """
    return _resolve_env_field(ENV_TENANT_ID)


@lru_cache(maxsize=1)
def _resolved_job_context() -> tuple[tuple[str, str], ...]:
    """Resolve and freeze the job context once per process.

    Returned as a tuple of ``(key, value)`` pairs so the cached value is
    immutable — callers materialize a fresh dict each call. Tests that
    mutate env vars can invalidate via ``resolve_job_context.cache_clear()``.
    """
    candidates = {
        "folderKey": _resolve_env_field(ENV_FOLDER_KEY),
        "jobKey": _resolve_env_field(ENV_JOB_KEY),
        "processKey": _resolve_env_field(ENV_PROCESS_KEY),
        "referenceId": _resolve_env_field(ENV_REFERENCE_ID),
        "agentVersion": _resolve_env_field(ENV_AGENT_VERSION),
    }
    return tuple((k, v) for k, v in candidates.items() if v)


def resolve_job_context() -> dict[str, str]:
    """Return the agent's job-execution context for the govern payload.

    Each field is read from its environment variable and only
    included when it resolves to a truthy value, so the server receives
    exactly the keys the agent actually knows. Cached per-process — the
    underlying values are immutable for the agent's lifetime. The server
    maps these onto the LLMOps trace record:

    - ``folderKey``    → ``FolderKey`` / ``uipath.folder_key``
    - ``jobKey``       → ``JobKey`` / ``uipath.job_key``
    - ``processKey``   → ``ProcessKey``
    - ``referenceId``  → ``ReferenceId`` (typically the agent id)
    - ``agentVersion`` → ``AgentVersion``
    """
    return dict(_resolved_job_context())


resolve_job_context.cache_clear = _resolved_job_context.cache_clear  # type: ignore[attr-defined]


# ----------------------------------------------------------------------------
# Generic safe-call helper. Used by callers that want "log and continue" on
# any unexpected failure path without spelling out the same try/except every
# time. The intentional GovernanceBlockException ALWAYS propagates — only
# this exception type carries policy intent; anything else is a bug.
# ----------------------------------------------------------------------------


def safe_call(
    fn: Callable[..., None],
    *args: object,
    what: str,
    **kwargs: object,
) -> None:
    """Call ``fn(*args, **kwargs)`` and swallow any non-block exception.

    ``GovernanceBlockException`` propagates (intentional policy block);
    everything else is logged at WARNING with the ``what`` label and
    swallowed so the agent can continue. Designed for fire-and-forget
    governance paths that should never fail an agent run.

    Args:
        fn: Callable to invoke.
        what: Short label used in the log line on failure
            (e.g. ``"BEFORE_AGENT governance check"``).
    """
    # Lazy import to avoid pulling uipath-core into module load.
    from uipath.core.governance.exceptions import GovernanceBlockException

    try:
        fn(*args, **kwargs)
    except GovernanceBlockException:
        raise
    except Exception as exc:  # noqa: BLE001 - fail-open by contract
        logger.warning("%s failed (continuing): %s", what, exc)
