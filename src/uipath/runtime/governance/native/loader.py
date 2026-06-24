"""Policy pack loader.

Per-runtime policy loading: a :class:`PolicyLoader` instance owns one
provider plus the cached PolicyIndex and prefetch state. The runtime
never contacts the governance backend directly; the provider owns the
wire / transport (auth, retries, telemetry). When no provider is
supplied, or the provider raises / returns an empty body / yields zero
rules, the loader returns an empty PolicyIndex and the agent runs
without any rules.

The loader holds **no module-level state**. ``uipath eval`` can spin up
multiple ``GovernanceRuntime`` instances in the same process and each
gets its own loader with its own provider, cache, and selector — no
cross-instance interference.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter

import yaml
from uipath.core.governance import GovernancePolicyProvider, PolicyContext

from uipath.runtime.governance.config import set_enforcement_mode
from uipath.runtime.governance.native._yaml_to_index import build_policy_index_from_yaml
from uipath.runtime.governance.native.models import PolicyIndex

logger = logging.getLogger(__name__)


class PolicyLoader:
    """Instance-scoped policy loader bound to one provider.

    Owns the policy-index cache, prefetch coordination, and the
    conversational selector for a single :class:`GovernanceRuntime`
    instance. Multiple loaders coexist in the same process without
    clobbering each other.

    Typical lifecycle::

        loader = PolicyLoader(provider, is_conversational=False)
        loader.prefetch()                  # non-blocking, optional
        index = loader.get_policy_index()  # cached after first call

    When ``provider`` is ``None``, every load returns an empty
    PolicyIndex without invoking anything.
    """

    # Upper bound on how long :meth:`get_policy_index` waits for an
    # in-flight prefetch before falling back to an empty PolicyIndex.
    # The provider owns its own transport timeouts; this is the runtime's
    # ceiling on blocking the first hook fire.
    _PROVIDER_WAIT_SECONDS = 10.0

    def __init__(
        self,
        provider: GovernancePolicyProvider | None,
        *,
        is_conversational: bool | None = None,
    ) -> None:
        """Construct a per-runtime policy loader.

        Args:
            provider: Policy source. ``None`` means no policies will be
                loaded — the loader yields an empty PolicyIndex.
            is_conversational: Whether the hosted agent is
                conversational. Travels in the :class:`PolicyContext`
                so the provider can select the matching policy view.
                ``None`` leaves the selector unset — the provider
                applies its default.
        """
        self._provider = provider
        self._is_conversational = is_conversational
        self._policy_index: PolicyIndex | None = None
        # ``_prefetch_event`` is set once the background load finishes
        # (success OR failure); callers of ``get_policy_index`` wait on
        # it. ``_prefetch_lock`` guards the start-once semantics so
        # concurrent ``prefetch`` calls don't kick off duplicate threads.
        self._prefetch_event: threading.Event | None = None
        self._prefetch_lock = threading.Lock()

    def prefetch(self) -> None:
        """Kick off a background load of the policy index.

        Non-blocking. Designed to be called as early as possible (at
        :class:`GovernanceRuntime` init) so the policy fetch overlaps
        with the rest of agent setup. The result lands in this loader's
        cache; :meth:`get_policy_index` waits on the prefetch when it's
        in flight.

        Idempotent: subsequent calls while the first is running are
        no-ops, and calls after completion are no-ops. No-op when no
        provider is supplied — there's nothing to fetch.
        """
        if self._provider is None:
            return

        with self._prefetch_lock:
            if self._policy_index is not None:
                return  # already loaded
            if self._prefetch_event is not None:
                return  # already in flight
            event = threading.Event()
            self._prefetch_event = event

        def _worker() -> None:
            try:
                loaded = self.load_policy_index()
            except Exception as exc:  # noqa: BLE001 - logged; first hook will retry sync
                logger.warning("Policy prefetch failed: %s", exc)
            else:
                with self._prefetch_lock:
                    # Only publish if we're still the live prefetch.
                    # ``clear_cache`` nulls ``_prefetch_event`` to retire
                    # an in-flight worker; in that case the loaded value
                    # belongs to a stale generation and must be dropped
                    # rather than clobbering the just-cleared state.
                    if self._prefetch_event is event:
                        self._policy_index = loaded
            finally:
                event.set()

        threading.Thread(
            target=_worker,
            name="governance-policy-prefetch",
            daemon=True,
        ).start()

    def get_policy_index(self) -> PolicyIndex:
        """Get the cached policy index, loading if necessary.

        Resolution order on first call:
          1. If a prefetch (see :meth:`prefetch`) is in flight, wait
             for it to complete (bounded by ``_PROVIDER_WAIT_SECONDS``).
          2. Synchronously call :meth:`load_policy_index` (which invokes
             the provider).
          3. Empty PolicyIndex when no provider is supplied or the
             provider fails / returns nothing.

        Result is cached for the loader's lifetime; per-hook evaluation
        never touches the network. Call :meth:`clear_cache` to force a
        refetch (mainly for tests).
        """
        if self._policy_index is not None:
            return self._policy_index

        event = self._prefetch_event
        if event is not None:
            completed = event.wait(timeout=self._PROVIDER_WAIT_SECONDS)
            if completed and self._policy_index is not None:
                return self._policy_index
            if not completed:
                # Timeout: cache an empty index so we don't re-wait the
                # full timeout on every subsequent hook.
                logger.warning(
                    "Policy prefetch did not complete in %.1fs; "
                    "agent will run without any policies",
                    self._PROVIDER_WAIT_SECONDS,
                )
                self._policy_index = PolicyIndex()
                return self._policy_index

            # Completed but produced no PolicyIndex — the worker hit an
            # unexpected error. Do NOT cache the empty result: caching
            # would permanently disable governance for the loader's
            # lifetime even though a later prefetch / clear_cache could
            # still recover. Return an empty index for this call only.
            logger.warning(
                "Policy prefetch completed but produced no PolicyIndex "
                "(see prior WARN for the root cause); agent will run "
                "without any policies for this call"
            )
            return PolicyIndex()

        # No prefetch was started (direct callers / tests). Sync load.
        self._policy_index = self.load_policy_index()
        return self._policy_index

    def load_policy_index(self) -> PolicyIndex:
        """Synchronously load and parse the policy index.

        Returns:
            PolicyIndex parsed from the provider response. Empty
            PolicyIndex when no provider is supplied, the provider
            raises, the YAML is malformed, or the response yields
            zero rules.
        """
        start = time.perf_counter()

        index = (
            self._load_from_provider(self._provider)
            if self._provider is not None
            else None
        )

        if index is not None:
            self._log_index_summary(index)
            logger.info(
                "Policy index ready: source=provider, total_ms=%.1f",
                (time.perf_counter() - start) * 1000,
            )
            return index

        reason = self._empty_index_reason()
        logger.info(
            "Policy index ready: source=empty (%s), total_ms=%.1f",
            reason,
            (time.perf_counter() - start) * 1000,
        )
        return PolicyIndex()

    def _empty_index_reason(self) -> str:
        """Diagnose why policy loading produced nothing."""
        if self._provider is None:
            return "no policy provider supplied"
        return "provider returned no policies (error / empty body / zero rules)"

    def _load_from_provider(
        self, provider: GovernancePolicyProvider
    ) -> PolicyIndex | None:
        """Fetch and parse the policy index via the supplied provider.

        Applies the provider-supplied enforcement mode as a side effect.
        Returns ``None`` when the provider raises, when the YAML is
        malformed, or when the resulting index has no rules — caller
        returns an empty PolicyIndex in those cases.

        Takes ``provider`` as a parameter (rather than reading
        ``self._provider``) so the type system can prove the call site
        is non-None — :meth:`load_policy_index` guards on ``None`` and
        passes the narrowed value through.
        """
        start = time.perf_counter()

        ctx = PolicyContext(is_conversational=self._is_conversational)

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

    def _log_index_summary(self, index: PolicyIndex) -> None:
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

    @property
    def available_packs(self) -> list[str]:
        """Pack names from the currently loaded policy index.

        Returns whatever the provider supplied on the most recent load.
        Empty list if no index has been loaded yet.
        """
        if self._policy_index is None:
            return []
        return self._policy_index.pack_names

    def clear_cache(self) -> None:
        """Clear the cached policy index and any in-flight prefetch state.

        Next call to :meth:`get_policy_index` will reload from the
        provider.
        """
        with self._prefetch_lock:
            self._policy_index = None
            self._prefetch_event = None
        logger.debug("Policy index cache cleared")
