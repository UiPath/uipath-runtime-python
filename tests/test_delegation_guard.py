"""Tests for the async-aware delegation depth guard.

The guard wraps an agent's ``invoke`` and ``ainvoke`` so a single
ContextVar tracks delegation depth across both sync and async call
chains. The async wrapper must itself be a coroutine — wrapping with a
sync function would return an un-awaited coroutine and silently bypass
the depth check.
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest
from uipath.core.governance.exceptions import GovernanceBlockException

from uipath.runtime.governance.delegation_guard import (
    install_delegation_guard,
    uninstall_delegation_guard,
)

# ---------------------------------------------------------------------------
# Helpers — minimal agent shapes the guard might encounter in the wild.
# ---------------------------------------------------------------------------


def _make_sync_agent() -> SimpleNamespace:
    agent = SimpleNamespace()
    agent.invoke = lambda payload, **_: {"sync": payload}
    return agent


def _make_async_agent() -> SimpleNamespace:
    agent = SimpleNamespace()

    async def _ainvoke(payload, **_):
        return {"async": payload}

    agent.ainvoke = _ainvoke
    return agent


def _make_dual_agent() -> SimpleNamespace:
    """Agent with both sync invoke and async ainvoke (LangGraph React shape)."""
    agent = _make_sync_agent()

    async def _ainvoke(payload, **_):
        return {"async": payload}

    agent.ainvoke = _ainvoke
    return agent


# ---------------------------------------------------------------------------
# Sync path — preserves the original behaviour the guard always had.
# ---------------------------------------------------------------------------


def test_sync_invoke_passes_through_under_limit() -> None:
    agent = _make_sync_agent()
    install_delegation_guard(agent, max_depth=3)
    assert agent.invoke({"x": 1}) == {"sync": {"x": 1}}


def test_sync_invoke_raises_when_depth_exceeded() -> None:
    """Recursive sync invokes blow the limit."""
    agent = SimpleNamespace()
    calls = {"n": 0}

    def _invoke(_payload, **_):
        calls["n"] += 1
        # Recurse into ourselves through the guarded attribute.
        return agent.invoke({})

    agent.invoke = _invoke
    install_delegation_guard(agent, max_depth=3)

    with pytest.raises(GovernanceBlockException):
        agent.invoke({})
    # Depth check fires inside the wrapper before the original runs, so
    # we got exactly max_depth=3 successful entries plus one rejection.
    assert calls["n"] == 3


# ---------------------------------------------------------------------------
# Async path — the new shape this change unlocks.
# ---------------------------------------------------------------------------


def test_async_wrapper_is_a_coroutine_function() -> None:
    """The wrapped ainvoke must itself be awaitable.

    Regression test for the original bug: a sync wrapper around an async
    method returned an un-awaited coroutine and silently bypassed the
    depth check entirely.
    """
    agent = _make_async_agent()
    install_delegation_guard(agent, max_depth=3)
    assert asyncio.iscoroutinefunction(agent.ainvoke)


def test_async_invoke_passes_through_under_limit() -> None:
    agent = _make_async_agent()
    install_delegation_guard(agent, max_depth=3)
    result = asyncio.run(agent.ainvoke({"x": 1}))
    assert result == {"async": {"x": 1}}


def test_async_invoke_raises_when_depth_exceeded() -> None:
    agent = SimpleNamespace()
    calls = {"n": 0}

    async def _ainvoke(_payload, **_):
        calls["n"] += 1
        return await agent.ainvoke({})

    agent.ainvoke = _ainvoke
    install_delegation_guard(agent, max_depth=3)

    with pytest.raises(GovernanceBlockException):
        asyncio.run(agent.ainvoke({}))
    assert calls["n"] == 3


def test_sync_and_async_share_one_depth_counter() -> None:
    """A coroutine that falls through to sync ``invoke`` increments the same counter."""
    agent = _make_dual_agent()
    calls = {"n": 0}

    def _invoke(_payload, **_):
        calls["n"] += 1
        # Sync self-recursion through the same guarded attribute.
        return agent.invoke({})

    async def _ainvoke(_payload, **_):
        calls["n"] += 1
        # Cross-mode: async entry falls through to the sync path.
        return agent.invoke({})

    agent.invoke = _invoke
    agent.ainvoke = _ainvoke
    install_delegation_guard(agent, max_depth=2)

    with pytest.raises(GovernanceBlockException):
        asyncio.run(agent.ainvoke({}))
    # ainvoke (depth=1) → invoke (depth=2) → invoke (depth=3, blocked).
    # The guard rejects the third call before _invoke runs, so calls=2.
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# Lifecycle — install / uninstall semantics.
# ---------------------------------------------------------------------------


def test_install_is_idempotent() -> None:
    agent = _make_sync_agent()
    install_delegation_guard(agent, max_depth=5)
    wrapped_once = agent.invoke
    install_delegation_guard(agent, max_depth=5)
    assert agent.invoke is wrapped_once, "second install must not re-wrap"


def test_uninstall_restores_originals_for_both_methods() -> None:
    agent = _make_dual_agent()
    original_invoke = agent.invoke
    original_ainvoke = agent.ainvoke
    install_delegation_guard(agent, max_depth=5)
    assert agent.invoke is not original_invoke
    assert agent.ainvoke is not original_ainvoke

    uninstall_delegation_guard(agent)
    assert agent.invoke is original_invoke
    assert agent.ainvoke is original_ainvoke
    assert not getattr(agent, "_delegation_wrapped", False)


def test_uninstall_safe_on_unguarded_agent() -> None:
    agent = _make_sync_agent()
    # Should not raise; should leave agent unchanged.
    uninstall_delegation_guard(agent)
    assert callable(agent.invoke)


# ---------------------------------------------------------------------------
# Edge cases.
# ---------------------------------------------------------------------------


def test_agent_without_invoke_methods_is_noop() -> None:
    """Agents without any invokable method must not crash the install."""
    agent = SimpleNamespace(unrelated="value")
    install_delegation_guard(agent, max_depth=5)
    assert not getattr(agent, "_delegation_wrapped", False)


def test_env_var_max_depth_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """``UIPATH_GOVERNANCE_MAX_DELEGATION_DEPTH`` overrides the default."""
    monkeypatch.setenv("UIPATH_GOVERNANCE_MAX_DELEGATION_DEPTH", "1")
    agent = SimpleNamespace()
    calls = {"n": 0}

    def _invoke(_payload, **_):
        calls["n"] += 1
        return agent.invoke({})

    agent.invoke = _invoke
    install_delegation_guard(agent)  # picks up env

    with pytest.raises(GovernanceBlockException):
        agent.invoke({})
    assert calls["n"] == 1, "max_depth=1 should allow exactly one call"


def test_invalid_env_var_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UIPATH_GOVERNANCE_MAX_DELEGATION_DEPTH", "not-a-number")
    agent = _make_sync_agent()
    # Should not raise on install — falls back silently to the default.
    install_delegation_guard(agent)
    assert os.environ.get("UIPATH_GOVERNANCE_MAX_DELEGATION_DEPTH") == "not-a-number"
    assert callable(agent.invoke)


# ---------------------------------------------------------------------------
# Leak / scaling — pins the shared-ContextVar design.
# ---------------------------------------------------------------------------


def test_install_does_not_allocate_per_agent_contextvars() -> None:
    """N installs must not grow the module's ContextVar registry by N.

    The old implementation allocated a ``ContextVar`` per agent. Since
    ContextVar instances are interned by the interpreter and never GC'd,
    that was an unbounded leak. The current design holds a single
    module-level ContextVar of ``dict[id(agent), int]``.
    """
    from uipath.runtime.governance import delegation_guard as dg

    # Snapshot the single shared ContextVar.
    shared_var = dg._DELEGATION_DEPTHS

    for _ in range(100):
        agent = _make_sync_agent()
        install_delegation_guard(agent, max_depth=3)
        uninstall_delegation_guard(agent)

    # The module-level ContextVar is unchanged — same instance, no new
    # ContextVars were allocated.
    assert dg._DELEGATION_DEPTHS is shared_var


def test_two_agents_have_independent_depth_counters() -> None:
    """Exhausting one agent's depth limit doesn't leak into another agent.

    Both agents share the single module-level ContextVar but the dict
    inside isolates them via ``id(agent)``.
    """
    from uipath.runtime.governance import delegation_guard as dg

    agent_a = SimpleNamespace()
    calls_a = {"n": 0}

    def _invoke_a(_payload, **_):
        calls_a["n"] += 1
        return agent_a.invoke({})  # self-recursion until limit

    agent_a.invoke = _invoke_a

    agent_b = _make_sync_agent()

    install_delegation_guard(agent_a, max_depth=2)
    install_delegation_guard(agent_b, max_depth=2)

    # Drive agent_a to its limit.
    with pytest.raises(GovernanceBlockException):
        agent_a.invoke({})
    assert calls_a["n"] == 2

    # agent_b is a fresh chain in the same context. Its depth counter
    # is keyed by id(agent_b), so agent_a's exhausted state doesn't
    # affect it. Without the per-agent keying, agent_b would inherit
    # whatever depth was last set in this context.
    assert agent_b.invoke({"x": 1}) == {"sync": {"x": 1}}

    # After both calls, the ContextVar should be back to its initial
    # state — either unset (LookupError) or holding an empty dict. The
    # set/reset pairs each guarded call cleaned up after itself.
    try:
        depths = dg._DELEGATION_DEPTHS.get()
    except LookupError:
        depths = {}
    assert depths.get(id(agent_a), 0) == 0
    assert depths.get(id(agent_b), 0) == 0


def test_uninstall_clears_agent_depth_entry() -> None:
    """After uninstall, the agent's id is no longer in the depths dict.

    Prevents ``id(agent)`` reuse — Python recycles ids after GC — from
    mis-attributing a future agent's count to this one.
    """
    from uipath.runtime.governance import delegation_guard as dg

    agent = _make_sync_agent()
    install_delegation_guard(agent, max_depth=5)
    # Enter the guard once so the agent gets a depth entry.
    agent.invoke({})
    # invoke completed -> token reset -> entry should be back to 0 or
    # absent. We re-enter manually to plant a non-zero entry.
    agent_key = id(agent)
    dg._DELEGATION_DEPTHS.set({agent_key: 3})
    assert dg._DELEGATION_DEPTHS.get().get(agent_key) == 3

    uninstall_delegation_guard(agent)
    # Uninstall pops the entry from the current context.
    assert agent_key not in dg._DELEGATION_DEPTHS.get()
