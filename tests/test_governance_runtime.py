"""Tests for :class:`UiPathGovernedRuntime` — pure resolved-policy wrapper.

The runtime takes an already-resolved :class:`PolicyIndex` +
:class:`EnforcementMode` at construction (the host fetched the policy
asynchronously via the :class:`GovernancePolicyProvider` and compiled
the YAML). Tests here confirm the wrapper holds the snapshot and
passes execution straight through to the delegate.
"""

from __future__ import annotations

from typing import Any

from uipath.core.governance import EnforcementMode

from uipath.runtime.governance.native import (
    build_policy_index_from_yaml,
)
from uipath.runtime.governance.native.models import PolicyIndex
from uipath.runtime.governance.runtime import UiPathGovernedRuntime

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


# ---------------------------------------------------------------------------
# build_policy_index_from_yaml — host-side compile path
# ---------------------------------------------------------------------------


def test_build_policy_index_from_yaml_compiles_pack() -> None:
    """The host uses this to turn the provider's YAML response into the snapshot."""
    index = build_policy_index_from_yaml(SIMPLE_POLICY_YAML)
    assert isinstance(index, PolicyIndex)
    assert index.total_rules == 1
    assert "provider-pack" in index.pack_names


def test_build_policy_index_from_yaml_empty_yields_empty_index() -> None:
    """Empty YAML compiles to an empty PolicyIndex — host can pass straight through."""
    index = build_policy_index_from_yaml("")
    assert isinstance(index, PolicyIndex)
    assert index.total_rules == 0


# ---------------------------------------------------------------------------
# UiPathGovernedRuntime — passthroughs
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


def _make_runtime(
    delegate: _StubDelegate | None = None,
    *,
    policy_index: PolicyIndex | None = None,
    enforcement_mode: EnforcementMode = EnforcementMode.AUDIT,
) -> UiPathGovernedRuntime:
    """Build a runtime with sensible test defaults."""
    return UiPathGovernedRuntime(
        delegate or _StubDelegate(),
        policy_index if policy_index is not None else PolicyIndex(),
        enforcement_mode,
    )


# ---------------------------------------------------------------------------
# Snapshot stored internally — not exposed as a public property
# ---------------------------------------------------------------------------


def test_resolved_policy_index_is_held_for_evaluator_use() -> None:
    """The wrapper stores the resolved snapshot; the evaluator reads it."""
    index = build_policy_index_from_yaml(SIMPLE_POLICY_YAML)
    runtime = _make_runtime(policy_index=index)
    # Internal attribute — verify the wrapper kept the exact instance.
    assert runtime._policy_index is index


def test_enforcement_mode_is_held_for_evaluator_use() -> None:
    """The wrapper stores the mode supplied at construction."""
    runtime = _make_runtime(enforcement_mode=EnforcementMode.ENFORCE)
    assert runtime._enforcement_mode is EnforcementMode.ENFORCE


def test_empty_policy_index_is_a_valid_construction() -> None:
    """``PolicyIndex()`` with no packs is acceptable — wrapper attaches without rules."""
    runtime = _make_runtime(policy_index=PolicyIndex())
    assert runtime._policy_index.total_rules == 0


# ---------------------------------------------------------------------------
# Passthrough behavior
# ---------------------------------------------------------------------------


async def test_governance_runtime_execute_delegates() -> None:
    delegate = _StubDelegate()
    runtime = _make_runtime(delegate)

    result = await runtime.execute({"x": 1})

    assert result == "result"
    assert delegate.execute_calls == [({"x": 1}, None)]


async def test_governance_runtime_stream_delegates() -> None:
    delegate = _StubDelegate()
    runtime = _make_runtime(delegate)

    events = [e async for e in runtime.stream({"x": 1})]

    assert events == ["a", "b"]
    assert delegate.stream_calls == [({"x": 1}, None)]


async def test_governance_runtime_schema_and_dispose_delegate() -> None:
    delegate = _StubDelegate()
    runtime = _make_runtime(delegate)

    assert await runtime.get_schema() == "schema"
    await runtime.dispose()
    assert delegate.schema_called
    assert delegate.disposed
