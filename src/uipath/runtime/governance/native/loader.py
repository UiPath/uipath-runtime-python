"""Policy pack loader.

Resolves the active PolicyIndex at startup. Policies are fetched
exclusively from the governance backend (``api/v1/policy``); there is
no local compiled fallback. When the backend is unavailable, the
access token is unset, or the fetch times out, the loader returns an
empty PolicyIndex and the agent runs without any rules.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import Counter

import yaml
from uipath.core.governance import EnforcementMode

from uipath.runtime.governance.config import set_enforcement_mode
from uipath.runtime.governance.native._yaml_to_index import build_policy_index_from_yaml
from uipath.runtime.governance.native.backend_client import ENV_ACCESS_TOKEN
from uipath.runtime.governance.native.models import PolicyIndex
from uipath.runtime.governance.native.policy_api_client import (
    ENV_ORGANIZATION_ID,
    ENV_TENANT_ID,
    POLICY_API_TIMEOUT_SECONDS,
    fetch_policy_response,
    resolve_organization_id,
    resolve_tenant_id,
)

logger = logging.getLogger(__name__)

# Pack name aliases for backward compatibility
PACK_ALIASES: dict[str, str] = {
    "owasp": "owasp_agentic",
    "hipaa": "hipaa_runtime",
    "soc2": "soc2_runtime",
    "nist": "nist_ai_rmf_runtime",
    "eu_ai": "eu_ai_act_runtime",
    "iso": "iso42001_runtime",
}


# Module-level cache
_policy_index: PolicyIndex | None = None

# Background-prefetch coordination. ``_prefetch_event`` is set once the
# background load_policy_index() call finishes (success OR failure);
# callers of ``get_policy_index()`` wait on it. ``_prefetch_lock``
# protects the start-once semantics so concurrent ``prefetch`` calls
# don't kick off duplicate threads.
_prefetch_event: threading.Event | None = None
_prefetch_lock = threading.Lock()

# Default wait when ``get_policy_index()`` blocks on an in-flight
# prefetch. Matched to the policy-API HTTP timeout so a stuck backend
# bounds the total time spent waiting at first hook fire to
# ~POLICY_API_TIMEOUT_SECONDS. If the wait expires we return an empty
# PolicyIndex — the agent runs without any policies rather than
# blocking further or retrying.
_PREFETCH_WAIT_SECONDS = POLICY_API_TIMEOUT_SECONDS


def prefetch_policy_index() -> None:
    """Kick off a background load of the policy index.

    Non-blocking. Designed to be called as early as possible (at
    ``GovernanceRuntime.__init__``) so the HTTP call to the governance
    backend overlaps with the rest of agent setup. Idempotent.
    """
    global _prefetch_event

    with _prefetch_lock:
        if _policy_index is not None:
            return  # already loaded
        if _prefetch_event is not None:
            return  # already in flight
        event = threading.Event()
        _prefetch_event = event

    def _worker() -> None:
        global _policy_index
        try:
            loaded = load_policy_index()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Policy prefetch failed: %s", exc)
        else:
            with _prefetch_lock:
                _policy_index = loaded
        finally:
            event.set()

    threading.Thread(
        target=_worker,
        name="governance-policy-prefetch",
        daemon=True,
    ).start()


def get_policy_index() -> PolicyIndex:
    """Get the cached policy index, loading if necessary.

    Resolution order on first call:
      1. If a prefetch (see :func:`prefetch_policy_index`) is in flight,
         wait for it to complete (bounded by ``_PREFETCH_WAIT_SECONDS``).
      2. Governance backend at ``api/v1/policy`` (one HTTP GET, cached).
      3. Empty PolicyIndex when the backend is unavailable or times out.

    Result is cached for the process lifetime; per-hook evaluation never
    touches the network. Call :func:`clear_policy_cache` to force a
    refetch (mainly for tests).
    """
    global _policy_index

    if _policy_index is not None:
        return _policy_index

    event = _prefetch_event
    if event is not None:
        completed = event.wait(timeout=_PREFETCH_WAIT_SECONDS)
        if completed and _policy_index is not None:
            return _policy_index
        if not completed:
            logger.warning(
                "Policy prefetch did not complete in %.1fs; "
                "agent will run without any policies",
                _PREFETCH_WAIT_SECONDS,
            )
        else:
            logger.warning(
                "Policy prefetch completed but produced no PolicyIndex "
                "(see prior WARN for the root cause); agent will run "
                "without any policies"
            )
        _policy_index = PolicyIndex()
        return _policy_index

    # No prefetch was started (direct callers / tests). Sync load.
    _policy_index = load_policy_index()
    return _policy_index


def load_policy_index(pack_name: str | None = None) -> PolicyIndex:
    """Load the active PolicyIndex from the governance backend.

    Args:
        pack_name: Ignored. Pack selection is controlled entirely by the
            backend.

    Returns:
        PolicyIndex parsed from the backend response. Empty PolicyIndex
        when the backend is unavailable, the token is unset, the YAML
        is malformed, or the response yields zero rules.
    """
    start = time.perf_counter()

    api_index = _load_from_api()
    if api_index is not None:
        _log_index_summary(api_index)
        logger.info(
            "Policy index ready: source=backend, total_ms=%.1f",
            (time.perf_counter() - start) * 1000,
        )
        return api_index

    reason = _empty_index_reason()
    logger.info(
        "Policy index ready: source=empty (%s), total_ms=%.1f",
        reason,
        (time.perf_counter() - start) * 1000,
    )
    return PolicyIndex()


def _empty_index_reason() -> str:
    """Diagnose why the policy fetch produced nothing."""
    if not resolve_organization_id():
        return (
            f"UiPathConfig.organization_id unavailable — set {ENV_ORGANIZATION_ID} "
            "or install uipath-platform; backend API not contacted"
        )
    if not resolve_tenant_id():
        return (
            f"UiPathConfig.tenant_id unavailable — set {ENV_TENANT_ID} "
            "or install uipath-platform; backend API not contacted"
        )
    if not os.environ.get(ENV_ACCESS_TOKEN):
        return f"{ENV_ACCESS_TOKEN} unset — backend API not contacted"
    return "backend returned no policies (timeout / error / empty body)"


def _apply_enforcement_mode(mode_str: str | None) -> None:
    """Map a backend-supplied mode string onto :class:`EnforcementMode`."""
    if not mode_str:
        return
    try:
        mode = EnforcementMode(mode_str.lower())
    except ValueError:
        logger.warning(
            "Backend returned unknown enforcement mode %r; keeping current mode",
            mode_str,
        )
        return
    set_enforcement_mode(mode)
    logger.info("Enforcement mode set from backend: %s", mode.value)


def _load_from_api() -> PolicyIndex | None:
    """Fetch and parse the policy index from the governance backend."""
    start = time.perf_counter()
    response = fetch_policy_response()
    if response is None:
        return None

    _apply_enforcement_mode(response.mode)

    if not response.policy:
        logger.warning(
            "Policy fetch returned empty policy field; "
            "agent will run without any policies"
        )
        return None

    try:
        index = build_policy_index_from_yaml(response.policy)
    except yaml.YAMLError as exc:
        logger.warning("Policy YAML from backend was malformed: %s", exc)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to build PolicyIndex from backend YAML: %s", exc)
        return None

    if index.total_rules == 0:
        logger.warning(
            "Policy YAML from backend yielded zero rules; "
            "agent will run without any policies"
        )
        return None

    elapsed_ms = (time.perf_counter() - start) * 1000
    logger.info(
        "Loaded policy index from backend: packs=%s, rules=%d, elapsed_ms=%.1f",
        index.pack_names,
        index.total_rules,
        elapsed_ms,
    )
    return index


def _log_index_summary(index: PolicyIndex) -> None:
    """Log summary of loaded policy index."""
    hook_counts: Counter[str] = Counter()
    for rule in index.all_rules:
        hook_counts[rule.hook.value] += 1

    logger.debug(
        "Policy packs: %s, total rules: %d, by hook: %s",
        index.pack_names,
        index.total_rules,
        dict(hook_counts),
    )


def get_available_packs() -> list[str]:
    """Get list of pack names from the currently loaded policy index."""
    if _policy_index is None:
        return []
    return _policy_index.pack_names


def clear_policy_cache() -> None:
    """Clear the cached policy index and any in-flight prefetch state."""
    global _policy_index, _prefetch_event
    with _prefetch_lock:
        _policy_index = None
        _prefetch_event = None
    logger.debug("Policy index cache cleared")


# Backward compatibility alias
reset_policy_index = clear_policy_cache
