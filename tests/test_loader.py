"""Tests for the policy loader module.

Provider-only world: the loader fetches policies exclusively through a
registered :class:`GovernancePolicyProvider`. Tests here cover the
caching, FF-gate, prefetch coordination, and fallback-to-empty behavior
that's independent of any specific provider. End-to-end provider
plumbing (mode application, YAML parsing, runtime wrapper integration)
lives in :mod:`tests.test_governance_runtime`.
"""

from __future__ import annotations

import threading
import time
from typing import Any
from unittest.mock import patch

import pytest
from uipath.core.governance import (
    EnforcementMode,
    PolicyContext,
    PolicyResponse,
)

from tests._helpers import StubPolicyProvider, reset_enforcement_mode
from uipath.runtime.governance.native import loader
from uipath.runtime.governance.native.loader import (
    _empty_index_reason,
    clear_policy_cache,
    get_available_packs,
    get_policy_index,
    load_policy_index,
    prefetch_policy_index,
    set_policy_provider,
)
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
    return PolicyResponse(
        mode=EnforcementMode.AUDIT, policies=SIMPLE_POLICY_YAML
    )


@pytest.fixture(autouse=True)
def _clean_loader_state():
    """Each test starts with a fresh loader cache and FF on."""
    from uipath.core.feature_flags import FeatureFlags

    clear_policy_cache()
    reset_enforcement_mode()
    set_policy_provider(None)
    FeatureFlags.configure_flags({"EnablePythonGovernanceChecker": True})
    yield
    clear_policy_cache()
    reset_enforcement_mode()
    set_policy_provider(None)
    FeatureFlags.reset_flags()


# ---------------------------------------------------------------------------
# _empty_index_reason
# ---------------------------------------------------------------------------


def test_empty_index_reason_no_provider() -> None:
    msg = _empty_index_reason()
    assert "no policy provider" in msg


def test_empty_index_reason_with_provider() -> None:
    set_policy_provider(StubPolicyProvider(response=_ok_response()))
    msg = _empty_index_reason()
    assert "provider returned no policies" in msg


# ---------------------------------------------------------------------------
# load_policy_index — public entry
# ---------------------------------------------------------------------------


def test_load_policy_index_empty_when_no_provider() -> None:
    """No provider registered → empty PolicyIndex."""
    index = load_policy_index()
    assert isinstance(index, PolicyIndex)
    assert index.total_rules == 0


def test_load_policy_index_uses_registered_provider() -> None:
    provider = StubPolicyProvider(response=_ok_response())
    set_policy_provider(provider)

    index = load_policy_index()

    assert isinstance(index, PolicyIndex)
    assert "test-pack" in index.pack_names
    assert len(provider.calls) == 1


def test_load_policy_index_returns_empty_when_provider_raises() -> None:
    set_policy_provider(StubPolicyProvider(raises=RuntimeError("boom")))
    index = load_policy_index()
    assert index.total_rules == 0


# ---------------------------------------------------------------------------
# get_policy_index — caching + FF gate
# ---------------------------------------------------------------------------


def test_get_policy_index_caches_after_first_call() -> None:
    """A second call returns the cached index without re-invoking the provider."""
    provider = StubPolicyProvider(response=_ok_response())
    set_policy_provider(provider)

    a = get_policy_index()
    b = get_policy_index()

    assert a is b
    assert len(provider.calls) == 1


def test_get_policy_index_short_circuits_when_ff_off() -> None:
    """FF off → return an empty index without invoking the provider."""
    from uipath.core.feature_flags import FeatureFlags

    FeatureFlags.configure_flags({"EnablePythonGovernanceChecker": False})
    provider = StubPolicyProvider(response=_ok_response())
    set_policy_provider(provider)

    index = get_policy_index()

    assert index.total_rules == 0
    assert provider.calls == []


def test_get_policy_index_sync_load_when_no_prefetch() -> None:
    """Without a prefetch in flight, get_policy_index synchronously loads."""
    set_policy_provider(StubPolicyProvider(response=_ok_response()))
    index = get_policy_index()
    assert index.total_rules == 1


# ---------------------------------------------------------------------------
# Prefetch — idempotency + completion + timeout
# ---------------------------------------------------------------------------


def test_prefetch_is_idempotent() -> None:
    """Second call while first is in flight is a no-op (no second thread)."""
    block = threading.Event()

    def _slow_get(context: PolicyContext) -> PolicyResponse:
        block.wait(timeout=2.0)
        return _ok_response()

    provider: Any = type("P", (), {"get_policy": staticmethod(_slow_get)})()
    set_policy_provider(provider)

    prefetch_policy_index()
    first_event = loader._prefetch_event
    prefetch_policy_index()
    assert loader._prefetch_event is first_event
    block.set()
    if first_event is not None:
        first_event.wait(timeout=2.0)


def test_prefetch_skipped_when_ff_off() -> None:
    """FF off → no prefetch thread started."""
    from uipath.core.feature_flags import FeatureFlags

    FeatureFlags.configure_flags({"EnablePythonGovernanceChecker": False})
    provider = StubPolicyProvider(response=_ok_response())
    set_policy_provider(provider)

    prefetch_policy_index()

    assert provider.calls == []
    assert loader._prefetch_event is None


def test_prefetch_no_op_when_index_already_loaded() -> None:
    """If the index is already cached, prefetch is a no-op."""
    provider = StubPolicyProvider(response=_ok_response())
    set_policy_provider(provider)
    get_policy_index()  # populate the cache

    prefetch_policy_index()

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
    set_policy_provider(provider)

    prefetch_policy_index()
    assert started.wait(timeout=2.0)
    threading.Thread(
        target=lambda: (time.sleep(0.05), release.set()), daemon=True
    ).start()
    index = get_policy_index()
    assert index.total_rules == 1


def test_get_policy_index_logs_when_prefetch_completes_with_empty_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 'completed but produced no PolicyIndex' branch fires on provider failure."""
    event = threading.Event()
    event.set()  # prefetch already completed
    monkeypatch.setattr(loader, "_prefetch_event", event)
    # _policy_index stays None — simulating "prefetch completed but produced nothing"
    with patch.object(loader.logger, "warning") as mock_warning:
        index = get_policy_index()
    assert index.total_rules == 0
    assert any(
        "completed but produced no PolicyIndex" in str(call.args[0])
        for call in mock_warning.call_args_list
    )


# ---------------------------------------------------------------------------
# get_available_packs / clear_policy_cache
# ---------------------------------------------------------------------------


def test_get_available_packs_before_load_returns_empty() -> None:
    assert get_available_packs() == []


def test_get_available_packs_after_load() -> None:
    set_policy_provider(StubPolicyProvider(response=_ok_response()))
    get_policy_index()
    assert "test-pack" in get_available_packs()


def test_clear_policy_cache_forces_refetch() -> None:
    provider = StubPolicyProvider(response=_ok_response())
    set_policy_provider(provider)

    get_policy_index()
    clear_policy_cache()
    get_policy_index()

    assert len(provider.calls) == 2


