"""Tests for the policy loader.

Provider-only world: each :class:`PolicyLoader` is instance-scoped and
bound to one :class:`GovernancePolicyProvider`. Tests here cover the
caching, prefetch coordination, and fallback-to-empty behavior
independent of any specific provider. End-to-end provider plumbing
(mode application, YAML parsing, runtime wrapper integration) lives in
:mod:`tests.test_governance_runtime`.

The loader no longer reads the governance feature flag — deciding
whether governance attaches at all is the wiring layer's concern, not
the loader's.
"""

from __future__ import annotations

import threading
import time
from typing import Any
from unittest.mock import patch

from uipath.core.governance import (
    EnforcementMode,
    PolicyContext,
    PolicyResponse,
)

from tests._helpers import StubPolicyProvider
from uipath.runtime.governance.native import loader as loader_mod
from uipath.runtime.governance.native.loader import PolicyLoader
from uipath.runtime.governance.native.models import PolicyIndex

SIMPLE_POLICY_YAML = """
standard: test-pack
version: "1.0"
rules:
  - id: r1
    hook: before_model
    checks:
      - type: regex
        patterns: ["leak"]
"""


def _ok_response() -> PolicyResponse:
    return PolicyResponse(mode=EnforcementMode.AUDIT, policies=SIMPLE_POLICY_YAML)


# Each test constructs a fresh ``PolicyLoader`` — no shared state to reset.


# ---------------------------------------------------------------------------
# _empty_index_reason — diagnostic string for the "no policies" log
# ---------------------------------------------------------------------------


def test_empty_index_reason_no_provider() -> None:
    msg = PolicyLoader(None)._empty_index_reason()
    assert "no policy provider" in msg


def test_empty_index_reason_with_provider() -> None:
    msg = PolicyLoader(StubPolicyProvider(response=_ok_response()))._empty_index_reason()
    assert "provider returned no policies" in msg


# ---------------------------------------------------------------------------
# load_policy_index — synchronous entry point
# ---------------------------------------------------------------------------


def test_load_policy_index_empty_when_no_provider() -> None:
    """No provider supplied → empty PolicyIndex."""
    index = PolicyLoader(None).load_policy_index()
    assert isinstance(index, PolicyIndex)
    assert index.total_rules == 0


def test_load_policy_index_uses_provider() -> None:
    provider = StubPolicyProvider(response=_ok_response())

    index = PolicyLoader(provider).load_policy_index()

    assert isinstance(index, PolicyIndex)
    assert "test-pack" in index.pack_names
    assert len(provider.calls) == 1


def test_load_policy_index_returns_empty_when_provider_raises() -> None:
    provider = StubPolicyProvider(raises=RuntimeError("boom"))
    index = PolicyLoader(provider).load_policy_index()
    assert index.total_rules == 0


# ---------------------------------------------------------------------------
# get_policy_index — caching
# ---------------------------------------------------------------------------


def test_get_policy_index_caches_after_first_call() -> None:
    """A second call returns the cached index without re-invoking the provider."""
    provider = StubPolicyProvider(response=_ok_response())
    loader = PolicyLoader(provider)

    a = loader.get_policy_index()
    b = loader.get_policy_index()

    assert a is b
    assert len(provider.calls) == 1


def test_get_policy_index_sync_load_when_no_prefetch() -> None:
    """Without a prefetch in flight, get_policy_index synchronously loads."""
    loader = PolicyLoader(StubPolicyProvider(response=_ok_response()))
    index = loader.get_policy_index()
    assert index.total_rules == 1


def test_get_policy_index_empty_with_no_provider() -> None:
    """No provider supplied → cached empty index, provider never invoked."""
    loader = PolicyLoader(None)
    a = loader.get_policy_index()
    b = loader.get_policy_index()
    assert a is b
    assert a.total_rules == 0


# ---------------------------------------------------------------------------
# Prefetch — idempotency + completion + timeout
# ---------------------------------------------------------------------------


def test_prefetch_no_op_when_provider_is_none() -> None:
    """No provider → prefetch is a no-op (no thread, no event)."""
    loader = PolicyLoader(None)
    loader.prefetch()
    assert loader._prefetch_event is None


def test_prefetch_is_idempotent() -> None:
    """Second call while first is in flight is a no-op (no second thread)."""
    block = threading.Event()

    def _slow_get(context: PolicyContext) -> PolicyResponse:
        block.wait(timeout=2.0)
        return _ok_response()

    provider: Any = type("P", (), {"get_policy": staticmethod(_slow_get)})()
    loader = PolicyLoader(provider)

    loader.prefetch()
    first_event = loader._prefetch_event
    loader.prefetch()
    assert loader._prefetch_event is first_event
    block.set()
    if first_event is not None:
        first_event.wait(timeout=2.0)


def test_prefetch_no_op_when_index_already_loaded() -> None:
    """If the index is already cached, prefetch is a no-op."""
    provider = StubPolicyProvider(response=_ok_response())
    loader = PolicyLoader(provider)
    loader.get_policy_index()  # populate the cache

    loader.prefetch()

    assert len(provider.calls) == 1


def test_get_policy_index_waits_for_prefetch_then_returns() -> None:
    """When a prefetch is in flight, get_policy_index waits for completion."""
    started = threading.Event()
    release = threading.Event()

    def _fetch(context: PolicyContext) -> PolicyResponse:
        started.set()
        release.wait(timeout=2.0)
        return _ok_response()

    provider: Any = type("P", (), {"get_policy": staticmethod(_fetch)})()
    loader = PolicyLoader(provider)

    loader.prefetch()
    assert started.wait(timeout=2.0)
    threading.Thread(
        target=lambda: (time.sleep(0.05), release.set()), daemon=True
    ).start()
    index = loader.get_policy_index()
    assert index.total_rules == 1


def test_get_policy_index_logs_when_prefetch_completes_with_empty_index() -> None:
    """The 'completed but produced no PolicyIndex' branch fires on provider failure.

    Manually wire a completed event without populating ``_policy_index`` —
    simulates a prefetch worker that hit an unexpected error after the
    event was claimed but before the index was set.
    """
    loader = PolicyLoader(StubPolicyProvider(response=_ok_response()))
    event = threading.Event()
    event.set()
    loader._prefetch_event = event

    with patch.object(loader_mod.logger, "warning") as mock_warning:
        index = loader.get_policy_index()

    assert index.total_rules == 0
    assert any(
        "completed but produced no PolicyIndex" in str(call.args[0])
        for call in mock_warning.call_args_list
    )


# ---------------------------------------------------------------------------
# available_packs / clear_cache
# ---------------------------------------------------------------------------


def test_available_packs_before_load_returns_empty() -> None:
    assert PolicyLoader(None).available_packs == []


def test_available_packs_after_load() -> None:
    loader = PolicyLoader(StubPolicyProvider(response=_ok_response()))
    loader.get_policy_index()
    assert "test-pack" in loader.available_packs


def test_clear_cache_forces_refetch() -> None:
    provider = StubPolicyProvider(response=_ok_response())
    loader = PolicyLoader(provider)

    loader.get_policy_index()
    loader.clear_cache()
    loader.get_policy_index()

    assert len(provider.calls) == 2


def test_clear_cache_drops_in_flight_worker_result() -> None:
    """A worker spawned before ``clear_cache`` must not clobber state after it.

    The race: ``prefetch()`` starts a worker, ``clear_cache()`` retires
    the prefetch event, then the worker finishes and (incorrectly,
    before the fix) writes its loaded index back over the cleared
    cache. With the fix the worker checks ``_prefetch_event is event``
    before publishing and discards its result when orphaned.
    """
    block = threading.Event()

    def _slow_get(context: PolicyContext) -> PolicyResponse:
        block.wait(timeout=2.0)
        return _ok_response()

    provider: Any = type("P", (), {"get_policy": staticmethod(_slow_get)})()
    loader = PolicyLoader(provider)

    loader.prefetch()
    captured_event = loader._prefetch_event
    assert captured_event is not None  # prefetch actually started

    # Retire the in-flight worker.
    loader.clear_cache()
    assert loader._policy_index is None
    assert loader._prefetch_event is None

    # Release the worker; let it finish and try to publish.
    block.set()
    assert captured_event.wait(timeout=2.0)

    # The orphan worker's result must NOT land in the cache.
    assert loader._policy_index is None


# ---------------------------------------------------------------------------
# Cross-instance isolation — the whole point of instance-scoped state
# ---------------------------------------------------------------------------


def test_two_loaders_do_not_share_cache() -> None:
    """Concurrent loaders maintain independent caches.

    ``uipath eval`` runs multiple runtimes in parallel; each gets its
    own loader and must not leak its cached PolicyIndex into the next.
    """
    p1 = StubPolicyProvider(response=_ok_response())
    p2 = StubPolicyProvider(response=_ok_response())
    l1 = PolicyLoader(p1)
    l2 = PolicyLoader(p2)

    l1.get_policy_index()
    l2.get_policy_index()

    assert len(p1.calls) == 1
    assert len(p2.calls) == 1


def test_two_loaders_carry_independent_conversational_selectors() -> None:
    """Each loader threads its own selector into PolicyContext."""
    p1 = StubPolicyProvider(response=_ok_response())
    p2 = StubPolicyProvider(response=_ok_response())
    PolicyLoader(p1, is_conversational=True).load_policy_index()
    PolicyLoader(p2, is_conversational=False).load_policy_index()

    assert p1.calls[0].is_conversational is True
    assert p2.calls[0].is_conversational is False
