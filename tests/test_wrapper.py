"""Tests for the governance feature-flag gate at the runtime boundary.

The FF-resolution logic itself lives in
``uipath.core.governance.config.is_governance_enabled`` and is tested
there. These tests only exercise the runtime's thin shim:
``apply_governance_wrapper`` defers to the gate, lazy-imports core's
``governance_wrapper`` only when the gate passes, and never lets
governance failures bring down the agent run.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest
from uipath.core.feature_flags import FeatureFlags

from uipath.runtime.wrapper import (
    GOVERNANCE_FEATURE_FLAG,
    apply_governance_wrapper,
)


@pytest.fixture(autouse=True)
def _reset_feature_flags():
    FeatureFlags.reset_flags()
    yield
    FeatureFlags.reset_flags()


@pytest.fixture
def fake_governance_module(monkeypatch):
    """Install a stub ``uipath.runtime.governance.wrapper`` for the lazy import.

    Each test gets its own MagicMock as ``governance_wrapper`` so we can
    assert call behaviour. Cleared by monkeypatch on teardown.
    """
    mock_wrapper = MagicMock(name="governance_wrapper")
    module = types.ModuleType("uipath.runtime.governance.wrapper")
    module.governance_wrapper = mock_wrapper

    monkeypatch.setitem(sys.modules, "uipath.runtime.governance.wrapper", module)
    return mock_wrapper


async def test_apply_governance_wrapper_returns_inner_when_flag_off(
    monkeypatch, fake_governance_module
):
    FeatureFlags.configure_flags({GOVERNANCE_FEATURE_FLAG: False})
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
