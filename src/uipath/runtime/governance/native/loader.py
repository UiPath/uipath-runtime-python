"""Policy pack loader.

Resolves the active PolicyIndex at startup by calling a registered
:class:`GovernancePolicyProvider`. The runtime never contacts the
governance backend directly; the provider owns the wire / transport
(auth, retries, telemetry). When no provider is registered, or the
provider raises / returns an empty body / yields zero rules, the
loader returns an empty PolicyIndex and the agent runs without any
rules.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter

import yaml
from uipath.core.governance import GovernancePolicyProvider, PolicyContext
from uipath.core.governance.config import is_governance_enabled

from uipath.runtime.governance.config import set_enforcement_mode
from uipath.runtime.governance.native._yaml_to_index import build_policy_index_from_yaml
from uipath.runtime.governance.native.models import PolicyIndex

logger = logging.getLogger(__name__)

# Module-level cache
_policy_index: PolicyIndex | None = None

# Background-prefetch coordination. ``_prefetch_event`` is set once the
# background load_policy_index() call finishes (success OR failure);
# callers of ``get_policy_index()`` wait on it. ``_prefetch_lock``
# protects the start-once semantics so concurrent ``prefetch`` calls
# don't kick off duplicate threads.
_prefetch_event: threading.Event | None = None
_prefetch_lock = threading.Lock()

# Upper bound on how long ``get_policy_index()`` waits for an in-flight
# prefetch before falling back to an empty PolicyIndex. The provider
# owns its own transport timeouts; this is the runtime's ceiling on
# blocking the first hook fire.
_PROVIDER_WAIT_SECONDS = 10.0

# Registered :class:`GovernancePolicyProvider`. Set by
# :class:`GovernanceRuntime` at init. ``None`` means no provider is
# registered — :func:`load_policy_index` returns an empty PolicyIndex
# in that case.
_policy_provider: GovernancePolicyProvider | None = None

# Whether the hosted agent is conversational. Travels in the
# :class:`PolicyContext` so the provider can select the matching policy
# view. A process-level holder (not a ContextVar) because the prefetch
# runs on a separate thread that wouldn't inherit one, and a
# coded-agent process hosts a single agent so the value is stable per
# process. ``None`` leaves the selector unset — the provider applies
# its default.
_agent_is_conversational: bool | None = None


def set_policy_provider(provider: GovernancePolicyProvider | None) -> None:
    """Register the policy provider the loader will use to fetch policies.

    Called once by :class:`GovernanceRuntime` during init before
    :func:`prefetch_policy_index`. ``None`` clears the registration —
    used by tests and by callers that opt out of governance.
    """
    global _policy_provider
    _policy_provider = provider


def set_agent_conversational(value: bool | None) -> None:
    """Record whether the hosted agent is conversational.

    Threaded into :class:`PolicyContext` on every provider call so the
    provider can resolve the conversational-vs-autonomous policy view.
    ``None`` clears the selector — the provider then applies its
    default.
    """
    global _agent_is_conversational
    _agent_is_conversational = value


def prefetch_policy_index() -> None:
    """Kick off a background load of the policy index.

    Non-blocking. Designed to be called as early as possible (at
    ``GovernanceRuntime.__init__``) so the policy fetch overlaps with
    the rest of agent setup. The result lands in the same module cache
    that ``get_policy_index()`` reads from; ``get_policy_index()`` waits
    on this prefetch when it's in flight.

    Idempotent: subsequent calls while the first is running are no-ops,
    and calls after completion are no-ops. Skipped entirely when the
    governance feature flag is OFF so the provider is never invoked.
    """
    global _prefetch_event

    if not is_governance_enabled():
        return

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
        except Exception as exc:  # noqa: BLE001 - logged; first hook will retry sync
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
      1. If the governance feature flag is OFF, return an empty
         PolicyIndex (cached). Provider is not invoked.
      2. If a prefetch (see :func:`prefetch_policy_index`) is in flight,
         wait for it to complete (bounded by ``_PROVIDER_WAIT_SECONDS``).
      3. Synchronously call :func:`load_policy_index` (which invokes the
         registered :class:`GovernancePolicyProvider`).
      4. Empty PolicyIndex when no provider is registered or the
         provider fails / returns nothing.

    Result is cached for the process lifetime; per-hook evaluation never
    touches the network. Call :func:`clear_policy_cache` to force a
    refetch (mainly for tests).
    """
    global _policy_index

    if _policy_index is not None:
        return _policy_index

    if not is_governance_enabled():
        logger.info(
            "Governance feature flag is OFF; returning empty PolicyIndex. "
            "No rules will fire. Set EnablePythonGovernanceChecker=True to enable."
        )
        _policy_index = PolicyIndex()
        return _policy_index

    event = _prefetch_event
    if event is not None:
        completed = event.wait(timeout=_PROVIDER_WAIT_SECONDS)
        if completed and _policy_index is not None:
            return _policy_index
        if not completed:
            # Timeout: deliberately cache an empty index so we don't
            # re-wait the full timeout on every subsequent hook.
            logger.warning(
                "Policy prefetch did not complete in %.1fs; "
                "agent will run without any policies",
                _PROVIDER_WAIT_SECONDS,
            )
            _policy_index = PolicyIndex()
            return _policy_index

        # Completed but produced no PolicyIndex — the worker hit an
        # unexpected error (provider failure, parse failure). Do NOT
        # cache the empty result: caching would permanently disable
        # governance for the process even though a later prefetch /
        # clear_policy_cache could still recover. Return an empty index
        # for this call only and leave the cache unset.
        logger.warning(
            "Policy prefetch completed but produced no PolicyIndex "
            "(see prior WARN for the root cause); agent will run "
            "without any policies for this call"
        )
        return PolicyIndex()

    # No prefetch was started (direct callers / tests). Sync load.
    _policy_index = load_policy_index()
    return _policy_index


def load_policy_index() -> PolicyIndex:
    """Load the active PolicyIndex via the registered policy provider.

    Returns:
        PolicyIndex parsed from the provider response. Empty PolicyIndex
        when no provider is registered, the provider raises, the YAML
        is malformed, or the response yields zero rules.
    """
    start = time.perf_counter()

    provider = _policy_provider
    index = _load_from_provider(provider) if provider is not None else None

    if index is not None:
        _log_index_summary(index)
        logger.info(
            "Policy index ready: source=provider, total_ms=%.1f",
            (time.perf_counter() - start) * 1000,
        )
        return index

    reason = _empty_index_reason()
    logger.info(
        "Policy index ready: source=empty (%s), total_ms=%.1f",
        reason,
        (time.perf_counter() - start) * 1000,
    )
    return PolicyIndex()


def _empty_index_reason() -> str:
    """Diagnose why policy loading produced nothing."""
    if _policy_provider is None:
        return "no policy provider registered"
    return "provider returned no policies (error / empty body / zero rules)"


def _load_from_provider(provider: GovernancePolicyProvider) -> PolicyIndex | None:
    """Fetch and parse the policy index via a :class:`GovernancePolicyProvider`.

    Applies the provider-supplied enforcement mode as a side effect.
    Returns ``None`` when the provider raises, when the YAML is
    malformed, or when the resulting index has no rules — caller returns
    an empty PolicyIndex in those cases.
    """
    start = time.perf_counter()

    ctx = PolicyContext(is_conversational=_agent_is_conversational)

    try:
        response = provider.get_policy(ctx)
    except Exception as exc:  # noqa: BLE001 - fail-open by contract
        logger.warning("Policy provider get_policy failed: %s", exc)
        return None

    if response.mode is not None:
        set_enforcement_mode(response.mode)
        logger.info("Enforcement mode set from provider: %s", response.mode.value)

    if not response.policies:
        logger.warning(
            "Policy provider returned empty policies field; "
            "agent will run without any policies"
        )
        return None

    try:
        index = build_policy_index_from_yaml(response.policies)
    except yaml.YAMLError as exc:
        logger.warning("Policy YAML from provider was malformed: %s", exc)
        return None
    except Exception as exc:  # noqa: BLE001 - never let load break agent startup
        logger.warning("Failed to build PolicyIndex from provider YAML: %s", exc)
        return None

    if index.total_rules == 0:
        logger.warning(
            "Policy YAML from provider yielded zero rules; "
            "agent will run without any policies"
        )
        return None

    elapsed_ms = (time.perf_counter() - start) * 1000
    logger.info(
        "Loaded policy index from provider: packs=%s, rules=%d, elapsed_ms=%.1f",
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
    """Get list of pack names from the currently loaded policy index.

    Returns whatever the provider supplied on the most recent load.
    Empty list if no index has been loaded yet.
    """
    if _policy_index is None:
        return []
    return _policy_index.pack_names


def clear_policy_cache() -> None:
    """Clear the cached policy index and any in-flight prefetch state.

    Next call to ``get_policy_index()`` will reload from the registered
    :class:`GovernancePolicyProvider`.
    """
    global _policy_index, _prefetch_event
    with _prefetch_lock:
        _policy_index = None
        _prefetch_event = None
    logger.debug("Policy index cache cleared")
