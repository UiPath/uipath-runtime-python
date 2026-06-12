"""Tests for the policy loader module.

Covers prefetch / get_policy_index / load_policy_index / _apply_enforcement_mode
plus the empty-index reason helper.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

import pytest
import yaml

from uipath.runtime.governance.config import (
    EnforcementMode,
    get_enforcement_mode,
    reset_enforcement_mode,
)
from uipath.runtime.governance.native import loader
from uipath.runtime.governance.native.loader import (
    _apply_enforcement_mode,
    _empty_index_reason,
    _load_from_api,
    clear_policy_cache,
    get_available_packs,
    get_policy_index,
    load_policy_index,
    prefetch_policy_index,
)
from uipath.runtime.governance.native.models import PolicyIndex
from uipath.runtime.governance.native.policy_api_client import PolicyResponse

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


@pytest.fixture(autouse=True)
def _clean_loader_state(monkeypatch: pytest.MonkeyPatch):
    """Each test starts with a fresh loader cache and a known env.

    Without this, tests leak the policy_index module global and
    `_prefetch_event` into one another.
    """
    clear_policy_cache()
    reset_enforcement_mode()
    # Enable the FF so the loader doesn't short-circuit immediately.
    from uipath.core.feature_flags import FeatureFlags

    FeatureFlags.configure_flags({"EnablePythonGovernanceChecker": True})
    monkeypatch.setenv("UIPATH_ORGANIZATION_ID", "org-1")
    monkeypatch.setenv("UIPATH_TENANT_ID", "tenant-1")
    monkeypatch.setenv("UIPATH_ACCESS_TOKEN", "tok")
    yield
    clear_policy_cache()
    reset_enforcement_mode()
    FeatureFlags.reset_flags()


# ---------------------------------------------------------------------------
# _empty_index_reason
# ---------------------------------------------------------------------------


def test_empty_index_reason_missing_org_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UIPATH_ORGANIZATION_ID", raising=False)
    msg = _empty_index_reason()
    assert "organization_id" in msg


def test_empty_index_reason_missing_tenant_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UIPATH_TENANT_ID", raising=False)
    msg = _empty_index_reason()
    assert "tenant_id" in msg


def test_empty_index_reason_missing_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UIPATH_ACCESS_TOKEN", raising=False)
    msg = _empty_index_reason()
    assert "UIPATH_ACCESS_TOKEN" in msg


def test_empty_index_reason_backend_returned_nothing() -> None:
    """All env present → reason is 'backend returned no policies'."""
    msg = _empty_index_reason()
    assert "backend returned no policies" in msg


# ---------------------------------------------------------------------------
# _apply_enforcement_mode
# ---------------------------------------------------------------------------


def test_apply_enforcement_mode_none_leaves_current() -> None:
    """Calling with ``None`` is a no-op — the existing mode is preserved."""
    from uipath.runtime.governance.config import set_enforcement_mode

    set_enforcement_mode(EnforcementMode.ENFORCE)
    _apply_enforcement_mode(None)
    assert get_enforcement_mode() == EnforcementMode.ENFORCE


def test_apply_enforcement_mode_empty_string_leaves_current() -> None:
    from uipath.runtime.governance.config import set_enforcement_mode

    set_enforcement_mode(EnforcementMode.AUDIT)
    _apply_enforcement_mode("")
    assert get_enforcement_mode() == EnforcementMode.AUDIT


@pytest.mark.parametrize(
    "mode_str,expected",
    [
        ("audit", EnforcementMode.AUDIT),
        ("enforce", EnforcementMode.ENFORCE),
        ("disabled", EnforcementMode.DISABLED),
        ("AUDIT", EnforcementMode.AUDIT),  # case-insensitive
    ],
)
def test_apply_enforcement_mode_known_values(
    mode_str: str, expected: EnforcementMode
) -> None:
    _apply_enforcement_mode(mode_str)
    assert get_enforcement_mode() == expected


def test_apply_enforcement_mode_unknown_value_keeps_current() -> None:
    from uipath.runtime.governance.config import set_enforcement_mode

    set_enforcement_mode(EnforcementMode.AUDIT)
    _apply_enforcement_mode("not-a-real-mode")
    # Mode is unchanged after the warning.
    assert get_enforcement_mode() == EnforcementMode.AUDIT


# ---------------------------------------------------------------------------
# _load_from_api
# ---------------------------------------------------------------------------


def test_load_from_api_returns_none_when_fetch_returns_none() -> None:
    with patch.object(loader, "fetch_policy_response", return_value=None):
        assert _load_from_api() is None


def test_load_from_api_returns_none_when_policy_is_empty() -> None:
    """A response with mode but empty policies field is treated as nothing."""
    response = PolicyResponse(mode="audit", policy="")
    with patch.object(loader, "fetch_policy_response", return_value=response):
        assert _load_from_api() is None


def test_load_from_api_applies_mode_then_parses() -> None:
    """The mode is applied BEFORE the YAML is parsed, so downstream sees it."""
    response = PolicyResponse(mode="enforce", policy=SIMPLE_POLICY_YAML)
    with patch.object(loader, "fetch_policy_response", return_value=response):
        index = _load_from_api()
    assert isinstance(index, PolicyIndex)
    assert index.total_rules == 1
    assert get_enforcement_mode() == EnforcementMode.ENFORCE


def test_load_from_api_swallows_yaml_error() -> None:
    """A malformed YAML body produces None, not an exception."""
    response = PolicyResponse(mode="audit", policy="key: : invalid: : yaml")
    with patch.object(loader, "fetch_policy_response", return_value=response):
        with patch.object(
            loader,
            "build_policy_index_from_yaml",
            side_effect=yaml.YAMLError("bad yaml"),
        ):
            assert _load_from_api() is None


def test_load_from_api_swallows_unexpected_exception() -> None:
    response = PolicyResponse(mode="audit", policy=SIMPLE_POLICY_YAML)
    with patch.object(loader, "fetch_policy_response", return_value=response):
        with patch.object(
            loader,
            "build_policy_index_from_yaml",
            side_effect=RuntimeError("library bug"),
        ):
            assert _load_from_api() is None


def test_load_from_api_returns_none_when_zero_rules() -> None:
    """YAML parses cleanly but yields no rules → treated as no-op."""
    empty_pack_yaml = "standard: empty\nrules: []\n"
    response = PolicyResponse(mode="audit", policy=empty_pack_yaml)
    with patch.object(loader, "fetch_policy_response", return_value=response):
        assert _load_from_api() is None


# ---------------------------------------------------------------------------
# load_policy_index — public entry
# ---------------------------------------------------------------------------


def test_load_policy_index_success_path() -> None:
    response = PolicyResponse(mode="audit", policy=SIMPLE_POLICY_YAML)
    with patch.object(loader, "fetch_policy_response", return_value=response):
        index = load_policy_index()
    assert isinstance(index, PolicyIndex)
    assert "test-pack" in index.pack_names


def test_load_policy_index_returns_empty_on_failure() -> None:
    """When the API yields None, the loader returns an empty PolicyIndex."""
    with patch.object(loader, "fetch_policy_response", return_value=None):
        index = load_policy_index()
    assert isinstance(index, PolicyIndex)
    assert index.total_rules == 0


# ---------------------------------------------------------------------------
# get_policy_index — caching + FF gate
# ---------------------------------------------------------------------------


def test_get_policy_index_caches_after_first_call() -> None:
    """A second call returns the cached index without re-fetching."""
    response = PolicyResponse(mode="audit", policy=SIMPLE_POLICY_YAML)
    with patch.object(
        loader, "fetch_policy_response", return_value=response
    ) as mock_fetch:
        a = get_policy_index()
        b = get_policy_index()
    assert a is b
    assert mock_fetch.call_count == 1


def test_get_policy_index_short_circuits_when_ff_off() -> None:
    """FF off → return an empty index without contacting the backend."""
    from uipath.core.feature_flags import FeatureFlags

    FeatureFlags.configure_flags({"EnablePythonGovernanceChecker": False})
    with patch.object(loader, "fetch_policy_response") as mock_fetch:
        index = get_policy_index()
    assert index.total_rules == 0
    assert not mock_fetch.called


def test_get_policy_index_sync_load_when_no_prefetch() -> None:
    """Without a prefetch in flight, get_policy_index synchronously loads."""
    response = PolicyResponse(mode="audit", policy=SIMPLE_POLICY_YAML)
    with patch.object(loader, "fetch_policy_response", return_value=response):
        index = get_policy_index()
    assert index.total_rules == 1


# ---------------------------------------------------------------------------
# Prefetch — idempotency + completion + timeout
# ---------------------------------------------------------------------------


def test_prefetch_is_idempotent() -> None:
    """Second call while first is in flight is a no-op (no second thread)."""
    block = threading.Event()

    def _slow_fetch():
        block.wait(timeout=2.0)
        return None

    with patch.object(loader, "fetch_policy_response", side_effect=_slow_fetch):
        prefetch_policy_index()
        first_event = loader._prefetch_event
        prefetch_policy_index()
        assert loader._prefetch_event is first_event
        # Let the worker finish so the autouse fixture's clear runs cleanly.
        block.set()
        if first_event is not None:
            first_event.wait(timeout=2.0)


def test_prefetch_skipped_when_ff_off() -> None:
    """FF off → no prefetch thread started."""
    from uipath.core.feature_flags import FeatureFlags

    FeatureFlags.configure_flags({"EnablePythonGovernanceChecker": False})
    with patch.object(loader, "fetch_policy_response") as mock_fetch:
        prefetch_policy_index()
    assert not mock_fetch.called
    assert loader._prefetch_event is None


def test_prefetch_no_op_when_index_already_loaded() -> None:
    """If the index is already cached, prefetch is a no-op."""
    response = PolicyResponse(mode="audit", policy=SIMPLE_POLICY_YAML)
    with patch.object(loader, "fetch_policy_response", return_value=response):
        get_policy_index()  # populate the cache
    with patch.object(loader, "fetch_policy_response") as mock_fetch:
        prefetch_policy_index()
    assert not mock_fetch.called


def test_get_policy_index_waits_for_prefetch_then_returns() -> None:
    """When a prefetch is in flight, get_policy_index waits for completion."""
    response = PolicyResponse(mode="audit", policy=SIMPLE_POLICY_YAML)
    started = threading.Event()
    release = threading.Event()

    def _fetch():
        started.set()
        release.wait(timeout=2.0)
        return response

    with patch.object(loader, "fetch_policy_response", side_effect=_fetch):
        prefetch_policy_index()
        assert started.wait(timeout=2.0)
        # Release the worker in a side thread so get_policy_index's wait
        # actually overlaps with the slow fetch.
        threading.Thread(
            target=lambda: (time.sleep(0.05), release.set()), daemon=True
        ).start()
        index = get_policy_index()
    assert index.total_rules == 1


def test_get_policy_index_logs_when_prefetch_completes_with_empty_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 'completed but produced no PolicyIndex' branch fires on auth/parse fail.

    Capturing via a logger mock instead of caplog because some
    test-isolation paths (other tests installing log interceptors)
    can prevent records from reaching caplog's root-attached handler.
    """
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
    response = PolicyResponse(mode="audit", policy=SIMPLE_POLICY_YAML)
    with patch.object(loader, "fetch_policy_response", return_value=response):
        get_policy_index()
    assert "test-pack" in get_available_packs()


def test_clear_policy_cache_forces_refetch() -> None:
    response = PolicyResponse(mode="audit", policy=SIMPLE_POLICY_YAML)
    with patch.object(
        loader, "fetch_policy_response", return_value=response
    ) as mock_fetch:
        get_policy_index()
        clear_policy_cache()
        get_policy_index()
    assert mock_fetch.call_count == 2


def test_reset_policy_index_alias_for_clear() -> None:
    """``reset_policy_index`` is the legacy alias for ``clear_policy_cache``."""
    assert loader.reset_policy_index is loader.clear_policy_cache
