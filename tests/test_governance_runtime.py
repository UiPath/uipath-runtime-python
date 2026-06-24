"""Tests for the GovernanceRuntime wrapper and the provider loader path.

The runtime no longer introspects the delegate's private attributes to
discover the conversational flag — the wiring layer passes it
explicitly. The runtime also no longer reads the governance feature
flag: the wiring layer decides whether to construct
:class:`GovernanceRuntime` at all.
"""

from __future__ import annotations

from typing import Any

import pytest
from uipath.core.governance import (
    EnforcementMode,
    PolicyResponse,
)

from tests._helpers import StubPolicyProvider, reset_enforcement_mode
from uipath.runtime.governance.config import get_enforcement_mode
from uipath.runtime.governance.native.loader import PolicyLoader
from uipath.runtime.governance.native.models import PolicyIndex
from uipath.runtime.governance.runtime import GovernanceRuntime

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
def _reset_mode() -> Any:
    """Each test starts with a clean enforcement-mode slate."""
    reset_enforcement_mode()
    yield
    reset_enforcement_mode()


# ---------------------------------------------------------------------------
# PolicyLoader — provider plumbing (mode application, context, errors)
# ---------------------------------------------------------------------------


def test_loader_builds_index_and_applies_mode() -> None:
    provider = StubPolicyProvider(
        response=PolicyResponse(mode=EnforcementMode.ENFORCE, policies=SIMPLE_POLICY_YAML)
    )

    index = PolicyLoader(provider).load_policy_index()

    assert isinstance(index, PolicyIndex)
    assert index.total_rules == 1
    assert "provider-pack" in index.pack_names
    assert get_enforcement_mode() == EnforcementMode.ENFORCE


def test_loader_passes_is_conversational_in_context() -> None:
    provider = StubPolicyProvider(
        response=PolicyResponse(mode=EnforcementMode.AUDIT, policies=SIMPLE_POLICY_YAML)
    )

    PolicyLoader(provider, is_conversational=True).load_policy_index()

    assert len(provider.calls) == 1
    assert provider.calls[0].is_conversational is True


def test_loader_omits_is_conversational_when_unset() -> None:
    """``is_conversational=None`` (the default) leaves the selector unset."""
    provider = StubPolicyProvider(
        response=PolicyResponse(mode=EnforcementMode.AUDIT, policies=SIMPLE_POLICY_YAML)
    )

    PolicyLoader(provider).load_policy_index()

    assert len(provider.calls) == 1
    assert provider.calls[0].is_conversational is None


def test_loader_returns_empty_when_provider_raises() -> None:
    provider = StubPolicyProvider(raises=RuntimeError("boom"))
    index = PolicyLoader(provider).load_policy_index()
    assert index.total_rules == 0


def test_loader_returns_empty_on_empty_policies() -> None:
    provider = StubPolicyProvider(
        response=PolicyResponse(mode=EnforcementMode.AUDIT, policies="")
    )
    index = PolicyLoader(provider).load_policy_index()
    assert index.total_rules == 0


def test_loader_returns_empty_on_zero_rules() -> None:
    empty_pack_yaml = "standard: empty\nrules: []\n"
    provider = StubPolicyProvider(
        response=PolicyResponse(mode=EnforcementMode.AUDIT, policies=empty_pack_yaml)
    )
    index = PolicyLoader(provider).load_policy_index()
    assert index.total_rules == 0


def test_loader_returns_empty_on_malformed_yaml() -> None:
    provider = StubPolicyProvider(
        response=PolicyResponse(
            mode=EnforcementMode.AUDIT, policies="key: : invalid: : yaml"
        )
    )
    index = PolicyLoader(provider).load_policy_index()
    assert index.total_rules == 0


def test_loader_does_not_change_mode_when_response_mode_is_none() -> None:
    from uipath.runtime.governance.config import set_enforcement_mode

    set_enforcement_mode(EnforcementMode.ENFORCE)
    provider = StubPolicyProvider(
        response=PolicyResponse(mode=None, policies=SIMPLE_POLICY_YAML)
    )

    PolicyLoader(provider).load_policy_index()

    assert get_enforcement_mode() == EnforcementMode.ENFORCE


# ---------------------------------------------------------------------------
# GovernanceRuntime — passthroughs + loader wiring
# ---------------------------------------------------------------------------


class _StubDelegate:
    """Captures delegate calls so the passthroughs can be asserted."""

    def __init__(self) -> None:
        self.execute_calls: list[tuple[Any, Any]] = []
        self.stream_calls: list[tuple[Any, Any]] = []
        self.disposed = False
        self.schema_called = False

    async def execute(self, input: Any = None, options: Any = None) -> Any:
        self.execute_calls.append((input, options))
        return "result"

    async def stream(self, input: Any = None, options: Any = None) -> Any:
        self.stream_calls.append((input, options))
        for event in ("a", "b"):
            yield event

    async def get_schema(self) -> Any:
        self.schema_called = True
        return "schema"

    async def dispose(self) -> None:
        self.disposed = True


def test_governance_runtime_exposes_loader_bound_to_provider() -> None:
    """The wrapper builds an instance-scoped PolicyLoader carrying the provider."""
    provider = StubPolicyProvider(
        response=PolicyResponse(mode=EnforcementMode.AUDIT, policies=SIMPLE_POLICY_YAML)
    )

    runtime = GovernanceRuntime(_StubDelegate(), policy_provider=provider)

    assert isinstance(runtime.loader, PolicyLoader)
    assert runtime.loader._provider is provider


def test_governance_runtime_forwards_is_conversational_to_loader() -> None:
    """The constructor's explicit ``is_conversational`` reaches PolicyContext."""
    provider = StubPolicyProvider(
        response=PolicyResponse(mode=EnforcementMode.AUDIT, policies=SIMPLE_POLICY_YAML)
    )

    runtime = GovernanceRuntime(
        _StubDelegate(), policy_provider=provider, is_conversational=True
    )
    # Force the prefetch to land — load synchronously so we can read calls[0].
    runtime.loader.get_policy_index()

    assert provider.calls, "provider.get_policy was never invoked"
    assert provider.calls[0].is_conversational is True


def test_governance_runtime_loader_default_selector_is_none() -> None:
    """Omitting ``is_conversational`` leaves the selector unset on PolicyContext."""
    provider = StubPolicyProvider(
        response=PolicyResponse(mode=EnforcementMode.AUDIT, policies=SIMPLE_POLICY_YAML)
    )

    runtime = GovernanceRuntime(_StubDelegate(), policy_provider=provider)
    runtime.loader.get_policy_index()

    assert provider.calls[0].is_conversational is None


def test_governance_runtime_with_none_provider_yields_empty_index() -> None:
    """No provider → loader yields an empty PolicyIndex, no provider invocation."""
    runtime = GovernanceRuntime(_StubDelegate(), policy_provider=None)

    index = runtime.loader.get_policy_index()
    assert index.total_rules == 0


async def test_governance_runtime_execute_delegates() -> None:
    delegate = _StubDelegate()
    runtime = GovernanceRuntime(delegate, policy_provider=None)

    result = await runtime.execute({"x": 1})

    assert result == "result"
    assert delegate.execute_calls == [({"x": 1}, None)]


async def test_governance_runtime_stream_delegates() -> None:
    delegate = _StubDelegate()
    runtime = GovernanceRuntime(delegate, policy_provider=None)

    events = [e async for e in runtime.stream({"x": 1})]

    assert events == ["a", "b"]
    assert delegate.stream_calls == [({"x": 1}, None)]


async def test_governance_runtime_schema_and_dispose_delegate() -> None:
    delegate = _StubDelegate()
    runtime = GovernanceRuntime(delegate, policy_provider=None)

    assert await runtime.get_schema() == "schema"
    await runtime.dispose()
    assert delegate.schema_called
    assert delegate.disposed
