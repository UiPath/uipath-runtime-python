"""Tests for delegation_guard.py — delegation depth tracking and guard installation."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from uipath.runtime.governance.delegation_guard import (
    _DELEGATION_DEPTHS,
    _build_violation,
    _current_depth,
    _enter_depth_if_under,
    _exit_depth,
    _resolve_max_depth,
    install_delegation_guard,
    uninstall_delegation_guard,
)

# ---------------------------------------------------------------------------
# _current_depth
# ---------------------------------------------------------------------------

def test_current_depth_returns_zero_when_no_context() -> None:
    # Reset the ContextVar to empty state by resetting to a fresh token
    token = _DELEGATION_DEPTHS.set({})
    try:
        assert _current_depth(999) == 0
    finally:
        _DELEGATION_DEPTHS.reset(token)


def test_current_depth_returns_stored_value() -> None:
    token = _DELEGATION_DEPTHS.set({42: 3})
    try:
        assert _current_depth(42) == 3
    finally:
        _DELEGATION_DEPTHS.reset(token)


# ---------------------------------------------------------------------------
# _enter_depth_if_under / _exit_depth
# ---------------------------------------------------------------------------

def test_enter_depth_increments_correctly() -> None:
    token_setup = _DELEGATION_DEPTHS.set({})
    try:
        new_depth, token = _enter_depth_if_under(100, 5)
        assert new_depth == 1
        assert token is not None
        _exit_depth(token)
    finally:
        _DELEGATION_DEPTHS.reset(token_setup)


def test_enter_depth_returns_none_token_when_limit_exceeded() -> None:
    token_setup = _DELEGATION_DEPTHS.set({100: 5})
    try:
        new_depth, token = _enter_depth_if_under(100, 5)
        assert new_depth == 6
        assert token is None
    finally:
        _DELEGATION_DEPTHS.reset(token_setup)


def test_enter_depth_tracks_multiple_agents() -> None:
    token_setup = _DELEGATION_DEPTHS.set({})
    try:
        _, tok1 = _enter_depth_if_under(1, 10)
        _, tok2 = _enter_depth_if_under(2, 10)
        assert _current_depth(1) == 1
        assert _current_depth(2) == 1
        assert tok1 is not None and tok2 is not None
        _exit_depth(tok2)
        _exit_depth(tok1)
    finally:
        _DELEGATION_DEPTHS.reset(token_setup)


def test_exit_depth_restores_previous() -> None:
    token_setup = _DELEGATION_DEPTHS.set({})
    try:
        _, tok = _enter_depth_if_under(55, 10)
        assert _current_depth(55) == 1
        assert tok is not None
        _exit_depth(tok)
        assert _current_depth(55) == 0
    finally:
        _DELEGATION_DEPTHS.reset(token_setup)


# ---------------------------------------------------------------------------
# _resolve_max_depth
# ---------------------------------------------------------------------------

def test_resolve_max_depth_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UIPATH_GOVERNANCE_MAX_DELEGATION_DEPTH", raising=False)
    assert _resolve_max_depth() == 25


def test_resolve_max_depth_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UIPATH_GOVERNANCE_MAX_DELEGATION_DEPTH", "10")
    assert _resolve_max_depth() == 10


def test_resolve_max_depth_invalid_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UIPATH_GOVERNANCE_MAX_DELEGATION_DEPTH", "notanumber")
    assert _resolve_max_depth() == 25


# ---------------------------------------------------------------------------
# _build_violation
# ---------------------------------------------------------------------------

def test_build_violation_creates_governance_block_exception() -> None:
    from uipath.core.governance.exceptions import GovernanceBlockException
    exc = _build_violation(6, 5)
    assert isinstance(exc, GovernanceBlockException)


# ---------------------------------------------------------------------------
# install_delegation_guard — sync invoke
# ---------------------------------------------------------------------------

def test_install_guard_wraps_invoke() -> None:
    agent = MagicMock()
    agent.invoke = MagicMock(return_value="result")
    del agent._delegation_wrapped  # ensure fresh agent

    install_delegation_guard(agent, max_depth=5)
    assert agent._delegation_wrapped is True
    result = agent.invoke("input")
    assert result == "result"


def test_install_guard_idempotent() -> None:
    agent = MagicMock()
    agent.invoke = MagicMock(return_value="ok")
    del agent._delegation_wrapped

    install_delegation_guard(agent, max_depth=5)
    original_invoke = agent.invoke
    install_delegation_guard(agent, max_depth=5)  # second call is a no-op
    assert agent.invoke is original_invoke


def test_install_guard_blocks_when_depth_exceeded() -> None:
    from uipath.core.governance.exceptions import GovernanceBlockException

    agent = MagicMock()
    calls: list[int] = []

    def invoke_that_recurses(data: object, **kwargs: object) -> str:
        calls.append(1)
        agent.invoke(data)  # recurse — depth will exceed max
        return "ok"

    agent.invoke = invoke_that_recurses
    del agent._delegation_wrapped
    install_delegation_guard(agent, max_depth=2)

    with pytest.raises(GovernanceBlockException):
        agent.invoke("start")


def test_install_guard_no_invoke_is_noop() -> None:
    agent = MagicMock(spec=[])  # no attributes at all
    install_delegation_guard(agent, max_depth=5)
    assert not getattr(agent, "_delegation_wrapped", False)


# ---------------------------------------------------------------------------
# install_delegation_guard — async ainvoke
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_install_guard_wraps_ainvoke() -> None:
    agent = MagicMock()

    async def original_ainvoke(data: object, **kwargs: object) -> str:
        return "async-result"

    agent.ainvoke = original_ainvoke
    del agent._delegation_wrapped
    install_delegation_guard(agent, max_depth=5)

    result = await agent.ainvoke("input")
    assert result == "async-result"


@pytest.mark.asyncio
async def test_install_guard_async_blocks_when_depth_exceeded() -> None:
    from uipath.core.governance.exceptions import GovernanceBlockException

    agent = MagicMock()

    async def ainvoke_recurse(data: object, **kwargs: object) -> str:
        await agent.ainvoke(data)
        return "done"

    agent.ainvoke = ainvoke_recurse
    del agent._delegation_wrapped
    install_delegation_guard(agent, max_depth=2)

    with pytest.raises(GovernanceBlockException):
        await agent.ainvoke("start")


# ---------------------------------------------------------------------------
# uninstall_delegation_guard
# ---------------------------------------------------------------------------

def test_uninstall_restores_original() -> None:
    original = MagicMock(return_value="orig")
    agent = MagicMock()
    agent.invoke = original
    del agent._delegation_wrapped

    install_delegation_guard(agent, max_depth=5)
    assert agent.invoke is not original

    uninstall_delegation_guard(agent)
    assert agent.invoke is original
    assert agent._delegation_wrapped is False


def test_uninstall_noop_on_unguarded_agent() -> None:
    agent = MagicMock()
    agent._delegation_wrapped = False
    uninstall_delegation_guard(agent)  # should not raise


def test_current_depth_returns_zero_when_var_unset() -> None:
    # Force a fresh context where _DELEGATION_DEPTHS has no value
    import contextvars
    ctx = contextvars.copy_context()
    # Run in a new context that hasn't set _DELEGATION_DEPTHS
    result = ctx.run(_current_depth, 12345)
    assert result == 0


def test_exit_depth_handles_value_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # Patch _DELEGATION_DEPTHS in the module to a mock that raises ValueError on reset
    from contextvars import ContextVar

    import uipath.runtime.governance.delegation_guard as dg_mod

    fake_var: ContextVar[dict[int, int]] = ContextVar("_test_fake")
    token = fake_var.set({})
    fake_var.reset(token)  # consume the token so reset raises ValueError next time

    class _BadVar:
        def reset(self, t: object) -> None:
            raise ValueError("foreign context")
        def get(self) -> dict[int, int]:
            return {}

    old = dg_mod._DELEGATION_DEPTHS
    monkeypatch.setattr(dg_mod, "_DELEGATION_DEPTHS", _BadVar())
    try:
        _exit_depth(token)  # should not raise; logs debug instead
    finally:
        monkeypatch.setattr(dg_mod, "_DELEGATION_DEPTHS", old)


def test_install_guard_uses_default_max_depth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UIPATH_GOVERNANCE_MAX_DELEGATION_DEPTH", raising=False)
    agent = MagicMock()
    agent.invoke = MagicMock(return_value="ok")
    del agent._delegation_wrapped
    install_delegation_guard(agent)  # max_depth=None uses env/default
    assert agent._delegation_wrapped is True


def test_install_guard_setattr_exception_logged(caplog: pytest.LogCaptureFixture) -> None:

    class _ReadOnlyInvoke:
        """Agent whose invoke is a data descriptor — setattr raises AttributeError."""
        @property
        def invoke(self) -> MagicMock:
            return MagicMock()

    agent = _ReadOnlyInvoke()
    # Just verify it doesn't raise (the AttributeError is caught and logged)
    install_delegation_guard(agent, max_depth=5)


def test_uninstall_setattr_exception_handled(caplog: pytest.LogCaptureFixture) -> None:

    class _Guarded:
        _delegation_wrapped = True
        _uipath_original_invoke = MagicMock()

        @property
        def invoke(self) -> MagicMock:
            return MagicMock()

        @invoke.setter
        def invoke(self, val: object) -> None:
            raise AttributeError("read-only")

    agent = _Guarded()
    uninstall_delegation_guard(agent)  # should not raise


def test_uninstall_delattr_missing_attr_handled() -> None:
    class _Guarded:
        _delegation_wrapped = True
        # Has NO _uipath_original_invoke attribute → delattr raises AttributeError

    agent = _Guarded()
    uninstall_delegation_guard(agent)  # should not raise


def test_uninstall_cleans_depth_entry() -> None:
    agent = MagicMock()
    agent.invoke = MagicMock(return_value="ok")
    del agent._delegation_wrapped

    agent_key = id(agent)
    token_setup = _DELEGATION_DEPTHS.set({agent_key: 2})
    try:
        install_delegation_guard(agent, max_depth=5)
        uninstall_delegation_guard(agent)
        # depth for this agent should be removed
        depths = _DELEGATION_DEPTHS.get()
        assert agent_key not in depths
    finally:
        try:
            _DELEGATION_DEPTHS.reset(token_setup)
        except Exception:
            pass
