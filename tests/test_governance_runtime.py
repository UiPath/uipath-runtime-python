"""Tests for the GovernanceRuntime wrapper and the provider loader path."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from uipath.core.governance import (
    EnforcementMode,
    PolicyResponse,
)

from tests._helpers import StubPolicyProvider, reset_enforcement_mode
from uipath.runtime.governance.config import get_enforcement_mode
from uipath.runtime.governance.native import loader
from uipath.runtime.governance.native.loader import (
    _load_from_provider,
    clear_policy_cache,
    load_policy_index,
    set_agent_conversational,
    set_policy_provider,
)
from uipath.runtime.governance.native.models import PolicyIndex
from uipath.runtime.governance.runtime import (
    GovernanceRuntime,
    _extract_is_conversational,
)

SIMPLE_POLICY_YAML = """
standard: provider-pack
version: "1.0"
rules:
  - id: r1
    hook: before_model
    checks:
      - type: regex
        patterns: ["leak"]
"""


@pytest.fixture(autouse=True)
def _enable_ff_and_reset(monkeypatch: pytest.MonkeyPatch):
    """Reset module state and turn the governance FF on per test."""
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
# _load_from_provider — direct unit tests
# ---------------------------------------------------------------------------


def test_load_from_provider_builds_index_and_applies_mode() -> None:
    provider = StubPolicyProvider(
        response=PolicyResponse(mode=EnforcementMode.ENFORCE, policies=SIMPLE_POLICY_YAML)
    )

    index = _load_from_provider(provider)

    assert isinstance(index, PolicyIndex)
    assert index.total_rules == 1
    assert "provider-pack" in index.pack_names
    assert get_enforcement_mode() == EnforcementMode.ENFORCE


def test_load_from_provider_passes_is_conversational_in_context() -> None:
    set_agent_conversational(True)
    provider = StubPolicyProvider(
        response=PolicyResponse(mode=EnforcementMode.AUDIT, policies=SIMPLE_POLICY_YAML)
    )

    _load_from_provider(provider)

    assert len(provider.calls) == 1
    assert provider.calls[0].is_conversational is True


def test_load_from_provider_returns_none_when_provider_raises() -> None:
    provider = StubPolicyProvider(raises=RuntimeError("boom"))

    assert _load_from_provider(provider) is None


def test_load_from_provider_returns_none_on_empty_policies() -> None:
    provider = StubPolicyProvider(
        response=PolicyResponse(mode=EnforcementMode.AUDIT, policies="")
    )

    assert _load_from_provider(provider) is None


def test_load_from_provider_returns_none_on_zero_rules() -> None:
    empty_pack_yaml = "standard: empty\nrules: []\n"
    provider = StubPolicyProvider(
        response=PolicyResponse(mode=EnforcementMode.AUDIT, policies=empty_pack_yaml)
    )

    assert _load_from_provider(provider) is None


def test_load_from_provider_returns_none_on_malformed_yaml() -> None:
    provider = StubPolicyProvider(
        response=PolicyResponse(
            mode=EnforcementMode.AUDIT, policies="key: : invalid: : yaml"
        )
    )

    assert _load_from_provider(provider) is None


def test_load_from_provider_does_not_change_mode_when_none() -> None:
    from uipath.runtime.governance.config import set_enforcement_mode

    set_enforcement_mode(EnforcementMode.ENFORCE)
    provider = StubPolicyProvider(
        response=PolicyResponse(mode=None, policies=SIMPLE_POLICY_YAML)
    )

    _load_from_provider(provider)

    assert get_enforcement_mode() == EnforcementMode.ENFORCE


# ---------------------------------------------------------------------------
# load_policy_index dispatch — registered provider vs empty fallback
# ---------------------------------------------------------------------------


def test_load_policy_index_uses_registered_provider() -> None:
    provider = StubPolicyProvider(
        response=PolicyResponse(mode=EnforcementMode.AUDIT, policies=SIMPLE_POLICY_YAML)
    )
    set_policy_provider(provider)

    index = load_policy_index()

    assert index.total_rules == 1
    assert provider.calls, "provider.get_policy was not called"


def test_load_policy_index_returns_empty_when_no_provider() -> None:
    """No provider registered → empty PolicyIndex (no fallback path)."""
    index = load_policy_index()
    assert index.total_rules == 0


def test_load_policy_index_empty_when_provider_yields_nothing() -> None:
    provider = StubPolicyProvider(
        response=PolicyResponse(mode=EnforcementMode.AUDIT, policies="")
    )
    set_policy_provider(provider)

    index = load_policy_index()

    assert index.total_rules == 0


# ---------------------------------------------------------------------------
# GovernanceRuntime
# ---------------------------------------------------------------------------


class _StubDelegate:
    """Captures delegate calls so the passthroughs can be asserted."""

    def __init__(self) -> None:
        self.execute_calls: list[tuple[Any, Any]] = []
        self.stream_calls: list[tuple[Any, Any]] = []
        self.disposed = False
        self.schema_called = False

    async def execute(self, input=None, options=None):
        self.execute_calls.append((input, options))
        return "result"

    async def stream(self, input=None, options=None):
        self.stream_calls.append((input, options))
        for event in ("a", "b"):
            yield event

    async def get_schema(self):
        self.schema_called = True
        return "schema"

    async def dispose(self):
        self.disposed = True


def test_governance_runtime_registers_provider_and_prefetches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Init wires provider into loader state and kicks off prefetch."""
    provider = StubPolicyProvider(
        response=PolicyResponse(mode=EnforcementMode.AUDIT, policies=SIMPLE_POLICY_YAML)
    )

    # Spy on prefetch + set_policy_provider so we don't need a real
    # background thread in the unit test.
    prefetch_spy = MagicMock()
    set_provider_spy = MagicMock()
    monkeypatch.setattr(
        "uipath.runtime.governance.runtime.prefetch_policy_index", prefetch_spy
    )
    monkeypatch.setattr(
        "uipath.runtime.governance.runtime.set_policy_provider", set_provider_spy
    )

    delegate = _StubDelegate()

    GovernanceRuntime(delegate, policy_provider=provider)

    set_provider_spy.assert_called_once_with(provider)
    prefetch_spy.assert_called_once_with()


def test_governance_runtime_with_none_provider_still_prefetches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Passing ``None`` registers None → loader yields an empty PolicyIndex."""
    prefetch_spy = MagicMock()
    set_provider_spy = MagicMock()
    monkeypatch.setattr(
        "uipath.runtime.governance.runtime.prefetch_policy_index", prefetch_spy
    )
    monkeypatch.setattr(
        "uipath.runtime.governance.runtime.set_policy_provider", set_provider_spy
    )

    GovernanceRuntime(_StubDelegate(), policy_provider=None)

    set_provider_spy.assert_called_once_with(None)
    prefetch_spy.assert_called_once_with()


def test_governance_runtime_skips_prefetch_when_ff_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FF off → no provider registration, no prefetch."""
    from uipath.core.feature_flags import FeatureFlags

    FeatureFlags.configure_flags({"EnablePythonGovernanceChecker": False})

    prefetch_spy = MagicMock()
    set_provider_spy = MagicMock()
    monkeypatch.setattr(
        "uipath.runtime.governance.runtime.prefetch_policy_index", prefetch_spy
    )
    monkeypatch.setattr(
        "uipath.runtime.governance.runtime.set_policy_provider", set_provider_spy
    )

    GovernanceRuntime(_StubDelegate(), policy_provider=StubPolicyProvider())

    assert not set_provider_spy.called
    assert not prefetch_spy.called


async def test_governance_runtime_execute_delegates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "uipath.runtime.governance.runtime.prefetch_policy_index", MagicMock()
    )
    monkeypatch.setattr(
        "uipath.runtime.governance.runtime.set_policy_provider", MagicMock()
    )
    delegate = _StubDelegate()
    runtime = GovernanceRuntime(delegate, policy_provider=None)

    result = await runtime.execute({"x": 1})

    assert result == "result"
    assert delegate.execute_calls == [({"x": 1}, None)]


async def test_governance_runtime_stream_delegates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "uipath.runtime.governance.runtime.prefetch_policy_index", MagicMock()
    )
    monkeypatch.setattr(
        "uipath.runtime.governance.runtime.set_policy_provider", MagicMock()
    )
    delegate = _StubDelegate()
    runtime = GovernanceRuntime(delegate, policy_provider=None)

    events = [e async for e in runtime.stream({"x": 1})]

    assert events == ["a", "b"]
    assert delegate.stream_calls == [({"x": 1}, None)]


async def test_governance_runtime_schema_and_dispose_delegate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "uipath.runtime.governance.runtime.prefetch_policy_index", MagicMock()
    )
    monkeypatch.setattr(
        "uipath.runtime.governance.runtime.set_policy_provider", MagicMock()
    )
    delegate = _StubDelegate()
    runtime = GovernanceRuntime(delegate, policy_provider=None)

    assert await runtime.get_schema() == "schema"
    await runtime.dispose()
    assert delegate.schema_called
    assert delegate.disposed


# ---------------------------------------------------------------------------
# _extract_is_conversational
# ---------------------------------------------------------------------------


def test_extract_is_conversational_true_from_agent_definition() -> None:
    delegate = SimpleNamespace(
        _agent_definition=SimpleNamespace(is_conversational=True)
    )
    assert _extract_is_conversational(delegate) is True


def test_extract_is_conversational_false_from_agent_definition() -> None:
    delegate = SimpleNamespace(
        _agent_definition=SimpleNamespace(is_conversational=False)
    )
    assert _extract_is_conversational(delegate) is False


def test_extract_is_conversational_returns_none_when_unreachable() -> None:
    """No ``_agent_definition`` anywhere on the chain → ``None`` (let the provider default)."""
    assert _extract_is_conversational(SimpleNamespace()) is None


def test_extract_is_conversational_returns_none_when_field_is_none() -> None:
    delegate = SimpleNamespace(
        _agent_definition=SimpleNamespace(is_conversational=None)
    )
    assert _extract_is_conversational(delegate) is None


def test_extract_is_conversational_unwraps_via_underscore_delegate() -> None:
    inner = SimpleNamespace(_agent_definition=SimpleNamespace(is_conversational=True))
    outer = SimpleNamespace(_delegate=inner)
    assert _extract_is_conversational(outer) is True


def test_extract_is_conversational_unwraps_via_delegate_attr() -> None:
    inner = SimpleNamespace(_agent_definition=SimpleNamespace(is_conversational=False))
    outer = SimpleNamespace(delegate=inner)
    assert _extract_is_conversational(outer) is False


def test_extract_is_conversational_depth_capped() -> None:
    """A pathological self-referential wrapper can't loop forever."""
    self_ref = SimpleNamespace()
    self_ref._delegate = self_ref  # type: ignore[attr-defined]
    assert _extract_is_conversational(self_ref) is None


# ---------------------------------------------------------------------------
# GovernanceRuntime wires the selector
# ---------------------------------------------------------------------------


def test_governance_runtime_sets_agent_type_from_delegate() -> None:
    """Init reads ``delegate._agent_definition.is_conversational`` and writes the selector."""
    delegate = SimpleNamespace(
        _agent_definition=SimpleNamespace(is_conversational=True),
        execute=_StubDelegate().execute,
        stream=_StubDelegate().stream,
        get_schema=_StubDelegate().get_schema,
        dispose=_StubDelegate().dispose,
    )

    # Don't run the real prefetch thread — just confirm the selector
    # ended up where the provider would read it.
    GovernanceRuntime(delegate, policy_provider=None)

    assert loader._agent_is_conversational is True


def test_governance_runtime_sets_none_when_agent_definition_missing() -> None:
    """No ``_agent_definition`` → selector stays unset (``None``)."""
    GovernanceRuntime(_StubDelegate(), policy_provider=None)
    assert loader._agent_is_conversational is None


def test_governance_runtime_preserves_externally_set_selector_on_extraction_miss() -> None:
    """Externally-set selector survives a runtime init that finds no ``_agent_definition``.

    Regression: previously ``__init__`` unconditionally wrote whatever
    ``_extract_is_conversational`` returned, so an extraction miss
    (``None``) silently clobbered a value an integration had pre-seeded.
    """
    set_agent_conversational(True)
    GovernanceRuntime(_StubDelegate(), policy_provider=None)
    assert loader._agent_is_conversational is True


def test_governance_runtime_fails_open_when_extraction_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pathological delegate accessor raising mid-extraction can't break init."""
    monkeypatch.setattr(
        "uipath.runtime.governance.runtime._extract_is_conversational",
        MagicMock(side_effect=RuntimeError("boom")),
    )
    set_provider_spy = MagicMock()
    prefetch_spy = MagicMock()
    monkeypatch.setattr(
        "uipath.runtime.governance.runtime.set_policy_provider", set_provider_spy
    )
    monkeypatch.setattr(
        "uipath.runtime.governance.runtime.prefetch_policy_index", prefetch_spy
    )

    # No exception escapes; the rest of init still runs.
    GovernanceRuntime(_StubDelegate(), policy_provider=None)

    set_provider_spy.assert_called_once_with(None)
    prefetch_spy.assert_called_once_with()


def test_governance_runtime_skips_extraction_when_ff_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FF off → no selector write, no provider registration, no prefetch."""
    from uipath.core.feature_flags import FeatureFlags

    FeatureFlags.configure_flags({"EnablePythonGovernanceChecker": False})

    extract_spy = MagicMock()
    monkeypatch.setattr(
        "uipath.runtime.governance.runtime._extract_is_conversational", extract_spy
    )

    delegate = SimpleNamespace(
        _agent_definition=SimpleNamespace(is_conversational=True),
        execute=_StubDelegate().execute,
        stream=_StubDelegate().stream,
        get_schema=_StubDelegate().get_schema,
        dispose=_StubDelegate().dispose,
    )
    GovernanceRuntime(delegate, policy_provider=None)

    assert not extract_spy.called
    assert loader._agent_is_conversational is None
