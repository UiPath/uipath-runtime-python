"""Tests for the governance feature-flag gate at the runtime boundary."""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest
from uipath.core.feature_flags import FeatureFlags

from uipath.runtime.wrapper import (
    GOVERNANCE_FEATURE_FLAG,
    _is_governance_enabled,
    apply_governance_wrapper,
)


@pytest.fixture(autouse=True)
def _reset_feature_flags():
    FeatureFlags.reset_flags()
    yield
    FeatureFlags.reset_flags()


@pytest.fixture
def fake_governance_module(monkeypatch):
    """Install a stub `uipath.governance.wrapper` so the lazy import resolves.

    Each test gets its own MagicMock as `governance_wrapper` so we can
    assert call behaviour. Cleared by monkeypatch on teardown.
    """
    mock_wrapper = MagicMock(name="governance_wrapper")
    module = types.ModuleType("uipath.governance.wrapper")
    module.governance_wrapper = mock_wrapper

    monkeypatch.setitem(sys.modules, "uipath.governance.wrapper", module)
    return mock_wrapper


def test_governance_enabled_when_flag_true():
    FeatureFlags.configure_flags({GOVERNANCE_FEATURE_FLAG: True})
    assert _is_governance_enabled() is True


def test_governance_disabled_when_flag_false():
    FeatureFlags.configure_flags({GOVERNANCE_FEATURE_FLAG: False})
    assert _is_governance_enabled() is False


def test_governance_disabled_when_flag_missing(monkeypatch):
    monkeypatch.delenv("UIPATH_FEATURE_" + GOVERNANCE_FEATURE_FLAG, raising=False)
    assert _is_governance_enabled() is False


def test_governance_env_var_enables_when_no_programmatic_value(monkeypatch):
    monkeypatch.setenv("UIPATH_FEATURE_" + GOVERNANCE_FEATURE_FLAG, "true")
    assert _is_governance_enabled() is True


def test_programmatic_value_beats_env_var(monkeypatch):
    monkeypatch.setenv("UIPATH_FEATURE_" + GOVERNANCE_FEATURE_FLAG, "true")
    FeatureFlags.configure_flags({GOVERNANCE_FEATURE_FLAG: False})
    assert _is_governance_enabled() is False


async def test_apply_governance_wrapper_returns_inner_when_flag_off(
    monkeypatch, fake_governance_module
):
    monkeypatch.delenv("UIPATH_FEATURE_" + GOVERNANCE_FEATURE_FLAG, raising=False)
    inner = MagicMock(name="InnerRuntime")

    result = await apply_governance_wrapper(inner, None, "runtime-1")

    assert result is inner
    fake_governance_module.assert_not_called()


async def test_apply_governance_wrapper_invokes_governance_when_flag_on(
    fake_governance_module,
):
    FeatureFlags.configure_flags({GOVERNANCE_FEATURE_FLAG: True})
    inner = MagicMock(name="InnerRuntime")
    governed = MagicMock(name="GovernedRuntime")
    fake_governance_module.return_value = governed

    result = await apply_governance_wrapper(inner, None, "runtime-1")

    assert result is governed
    fake_governance_module.assert_called_once_with(inner, None, "runtime-1")


async def test_apply_governance_wrapper_swallows_wrapper_exception(
    fake_governance_module,
):
    """If governance_wrapper raises, fail-safe: return inner unchanged."""
    FeatureFlags.configure_flags({GOVERNANCE_FEATURE_FLAG: True})
    fake_governance_module.side_effect = RuntimeError("boom")
    inner = MagicMock(name="InnerRuntime")

    result = await apply_governance_wrapper(inner, None, "runtime-1")

    assert result is inner
