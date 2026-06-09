"""Tests for step-isolated ``GovernanceRuntime.dispose()`` cleanup.

A single failing step in dispose() must not strand the remaining steps.
``self._delegate.dispose()`` always runs last and is the only step whose
exception propagates.
"""

from __future__ import annotations

import asyncio

import pytest
from uipath.core.feature_flags import FeatureFlags

from uipath.runtime.governance import wrapper as wrapper_mod
from uipath.runtime.governance.wrapper import GovernanceRuntime, _current_model_name
from uipath.runtime.wrapper import GOVERNANCE_FEATURE_FLAG


@pytest.fixture(autouse=True)
def _enable_governance():
    """These tests exercise the post-FF-gate dispose path."""
    FeatureFlags.configure_flags({GOVERNANCE_FEATURE_FLAG: True})
    yield
    FeatureFlags.reset_flags()


class _Holder:
    """Mutable attribute holder whose setattr can be configured to raise."""

    def __init__(self) -> None:
        self._raise_on_setattr = False
        self._setattr_count = 0
        # Pre-create the attr so the test isn't tripped by the agent
        # restore path being the "first" set.
        object.__setattr__(self, "agent", None)

    def __setattr__(self, name: str, value) -> None:  # type: ignore[no-untyped-def]
        if name in {"_raise_on_setattr", "_setattr_count"}:
            object.__setattr__(self, name, value)
            return
        object.__setattr__(self, "_setattr_count", self._setattr_count + 1)
        if self._raise_on_setattr:
            raise RuntimeError("restore failed")
        object.__setattr__(self, name, value)


class _Delegate:
    """Minimal delegate with an instrumented dispose()."""

    def __init__(self, *, raises: bool = False) -> None:
        self._dispose_count = 0
        self._raises = raises

    async def dispose(self) -> None:
        self._dispose_count += 1
        if self._raises:
            raise RuntimeError("delegate dispose failed")


def _make_runtime(
    *,
    restore_raises: bool = False,
    uninstall_raises: bool = False,
    delegate_dispose_raises: bool = False,
    monkeypatch: pytest.MonkeyPatch | None = None,
) -> tuple[GovernanceRuntime, _Holder, _Delegate, dict[str, int]]:
    """Build a runtime whose three dispose steps each have a tunable failure."""
    counters = {"uninstall": 0}
    delegate = _Delegate(raises=delegate_dispose_raises)
    holder = _Holder()
    holder._raise_on_setattr = restore_raises

    runtime = GovernanceRuntime(delegate=delegate, context=None, runtime_id="rid-1")

    # Inject the original-agent scaffolding so dispose tries to restore.
    runtime._original_agent = object()
    runtime._agent_attr_name = "agent"
    runtime._agent_holder = holder
    # Bind a fresh token so dispose has something to reset.
    runtime._model_name_token = _current_model_name.set("model-x")

    def _fake_uninstall(_agent) -> None:
        counters["uninstall"] += 1
        if uninstall_raises:
            raise RuntimeError("uninstall failed")

    assert monkeypatch is not None
    monkeypatch.setattr(wrapper_mod, "uninstall_delegation_guard", _fake_uninstall)

    return runtime, holder, delegate, counters


def test_dispose_runs_all_steps_when_each_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, holder, delegate, counters = _make_runtime(monkeypatch=monkeypatch)
    asyncio.run(runtime.dispose())
    assert holder._setattr_count == 1, "agent restore should have run once"
    assert counters["uninstall"] == 1
    assert delegate._dispose_count == 1
    assert runtime._model_name_token is None, "token must be cleared after dispose"


def test_dispose_continues_when_restore_step_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restore failure must not skip uninstall, var reset, or delegate dispose."""
    runtime, holder, delegate, counters = _make_runtime(
        restore_raises=True, monkeypatch=monkeypatch,
    )
    asyncio.run(runtime.dispose())
    assert holder._setattr_count == 1, "restore was attempted"
    assert counters["uninstall"] == 1, "uninstall must still run"
    assert delegate._dispose_count == 1, "delegate dispose must still run"
    assert runtime._model_name_token is None


def test_dispose_continues_when_uninstall_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Uninstall failure must not skip var reset or delegate dispose."""
    runtime, holder, delegate, counters = _make_runtime(
        uninstall_raises=True, monkeypatch=monkeypatch,
    )
    asyncio.run(runtime.dispose())
    assert holder._setattr_count == 1
    assert counters["uninstall"] == 1
    assert delegate._dispose_count == 1
    assert runtime._model_name_token is None


def test_dispose_propagates_delegate_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Delegate dispose failure surfaces to the caller — that's the contract."""
    runtime, holder, delegate, counters = _make_runtime(
        delegate_dispose_raises=True, monkeypatch=monkeypatch,
    )
    with pytest.raises(RuntimeError, match="delegate dispose failed"):
        asyncio.run(runtime.dispose())
    # All preceding governance steps must still have run.
    assert holder._setattr_count == 1
    assert counters["uninstall"] == 1
    assert delegate._dispose_count == 1
    assert runtime._model_name_token is None
