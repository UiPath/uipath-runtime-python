"""Tests for the runtime wrapper registry and the governance feature-flag gate."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from uipath.core.feature_flags import FeatureFlags

from uipath.runtime.wrapper import (
    ALLOWED_WRAPPERS,
    GOVERNANCE_FEATURE_FLAG,
    UiPathRuntimeWrapperRegistry,
    _is_governance_enabled,
)


@pytest.fixture(autouse=True)
def _reset_feature_flags():
    FeatureFlags.reset_flags()
    yield
    FeatureFlags.reset_flags()


def _make_registry_with_wrapper(name: str, wrapper):
    registry = UiPathRuntimeWrapperRegistry()
    # Bypass entry-point discovery by registering directly via the public API.
    # This requires the name to be in ALLOWED_WRAPPERS.
    assert name in ALLOWED_WRAPPERS
    registry.register(name, wrapper, priority=50)
    registry._entry_points_loaded = True  # skip entry-point scanning
    return registry


def test_governance_enabled_when_flag_true():
    FeatureFlags.configure_flags({GOVERNANCE_FEATURE_FLAG: True})
    assert _is_governance_enabled() is True


def test_governance_disabled_when_flag_false():
    FeatureFlags.configure_flags({GOVERNANCE_FEATURE_FLAG: False})
    assert _is_governance_enabled() is False


def test_governance_disabled_when_flag_missing(monkeypatch):
    # Drop any local env-var override so the test is hermetic.
    monkeypatch.delenv("UIPATH_FEATURE_" + GOVERNANCE_FEATURE_FLAG, raising=False)
    # FeatureFlags reset by the autouse fixture; nothing configured.
    assert _is_governance_enabled() is False


def test_governance_env_var_enables_when_no_programmatic_value(monkeypatch):
    """UIPATH_FEATURE_<name>=true enables governance when nothing is
    programmatically configured. This is core's documented fallback
    behaviour — useful for local dev / repros.
    """
    monkeypatch.setenv("UIPATH_FEATURE_" + GOVERNANCE_FEATURE_FLAG, "true")
    assert _is_governance_enabled() is True


def test_programmatic_value_beats_env_var(monkeypatch):
    """configure_flags() takes precedence over the env-var fallback."""
    monkeypatch.setenv("UIPATH_FEATURE_" + GOVERNANCE_FEATURE_FLAG, "true")
    FeatureFlags.configure_flags({GOVERNANCE_FEATURE_FLAG: False})
    assert _is_governance_enabled() is False


async def test_wrap_runtime_skips_governance_when_flag_disabled():
    FeatureFlags.configure_flags({GOVERNANCE_FEATURE_FLAG: False})

    called = []

    def fake_governance_wrapper(runtime, context, runtime_id):
        called.append(("governance", runtime_id))
        return MagicMock(name="GovernedRuntime")

    registry = _make_registry_with_wrapper("governance", fake_governance_wrapper)
    inner = MagicMock(name="InnerRuntime")

    result = await registry.wrap_runtime(inner, None, "runtime-1")

    assert result is inner  # not wrapped
    assert called == []  # governance wrapper never invoked


async def test_wrap_runtime_applies_governance_when_flag_enabled():
    FeatureFlags.configure_flags({GOVERNANCE_FEATURE_FLAG: True})

    called = []

    def fake_governance_wrapper(runtime, context, runtime_id):
        called.append(("governance", runtime_id))
        wrapped = MagicMock(name="GovernedRuntime")
        wrapped._inner = runtime
        return wrapped

    registry = _make_registry_with_wrapper("governance", fake_governance_wrapper)
    inner = MagicMock(name="InnerRuntime")

    result = await registry.wrap_runtime(inner, None, "runtime-1")

    assert result is not inner  # wrapped
    assert len(called) == 1
    assert called[0] == ("governance", "runtime-1")


async def test_wrap_runtime_with_no_wrappers_returns_runtime_unchanged():
    registry = UiPathRuntimeWrapperRegistry()
    registry._entry_points_loaded = True
    inner = MagicMock(name="InnerRuntime")

    result = await registry.wrap_runtime(inner, None, "runtime-1")

    assert result is inner
