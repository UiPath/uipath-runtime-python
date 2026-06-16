"""Delegation depth guard.

Patches an agent's ``invoke`` method to track recursion depth and raise
a ``GovernanceBlockException`` when the configured maximum is exceeded.
This prevents runaway sub-agent chains.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
from contextvars import ContextVar, Token
from typing import Any

from uipath.core.governance.exceptions import (
    GovernanceBlockException,
    GovernanceViolation,
)

logger = logging.getLogger(__name__)

_DEFAULT_MAX_DELEGATION_DEPTH = 25
_ENV_MAX_DELEGATION_DEPTH = "UIPATH_GOVERNANCE_MAX_DELEGATION_DEPTH"

# Single module-level ContextVar holding per-agent delegation depths
# keyed by ``id(agent)``. Each install / uninstall pair shares this one
# ContextVar instead of allocating a new one per agent — the interpreter
# interns ContextVars and never GCs them, so per-agent allocation was an
# unbounded leak in long-running hosts (every `install_delegation_guard`
# call permanently grew the interpreter's ContextVar registry).
#
# Per-context isolation (asyncio task / thread) still works the standard
# ContextVar way: each context sees its own copy of the depths dict, and
# nested invokes use ``set`` / ``reset`` for LIFO depth tracking. The
# dict itself is copied on every increment (copy-on-write) so concurrent
# contexts don't share state through a mutable mapping.
_DELEGATION_DEPTHS: ContextVar[dict[int, int]] = ContextVar(
    "_uipath_delegation_depths"
)


def _current_depth(agent_key: int) -> int:
    """Return the current depth for ``agent_key`` in this context."""
    try:
        return _DELEGATION_DEPTHS.get().get(agent_key, 0)
    except LookupError:
        return 0


def _enter_depth_if_under(
    agent_key: int, max_depth: int
) -> tuple[int, Token[dict[int, int]] | None]:
    """Attempt to increment depth for ``agent_key``.

    Returns ``(new_depth, token)`` where ``token`` is ``None`` if the
    new depth would exceed ``max_depth`` — caller raises and does not
    need to clean up. On success, caller must reset via ``token``.
    """
    try:
        depths = _DELEGATION_DEPTHS.get()
    except LookupError:
        depths = {}
    new_depth = depths.get(agent_key, 0) + 1
    if new_depth > max_depth:
        return new_depth, None
    new_depths = dict(depths)
    new_depths[agent_key] = new_depth
    token = _DELEGATION_DEPTHS.set(new_depths)
    return new_depth, token


def _exit_depth(token: Token[dict[int, int]]) -> None:
    """Undo a successful :func:`_enter_depth_if_under` call.

    Tolerates cross-context resets (token created in a different
    context — happens when a child task awaits an agent invoke) by
    accepting the leak rather than crashing the agent on dispose.
    """
    try:
        _DELEGATION_DEPTHS.reset(token)
    except (ValueError, LookupError):
        logger.debug("Delegation depth reset from foreign context")


def _resolve_max_depth() -> int:
    """Read max-depth from env at install time, falling back to default on parse error.

    Called once from :func:`install_delegation_guard`; the resolved value is
    captured per agent (``resolved_max``), so changing the env var after the
    guard is installed has no effect on already-wrapped agents.
    """
    raw = os.getenv(_ENV_MAX_DELEGATION_DEPTH)
    if raw is None:
        return _DEFAULT_MAX_DELEGATION_DEPTH
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            "Invalid %s=%r; using default %d",
            _ENV_MAX_DELEGATION_DEPTH,
            raw,
            _DEFAULT_MAX_DELEGATION_DEPTH,
        )
        return _DEFAULT_MAX_DELEGATION_DEPTH


def _build_violation(current: int, resolved_max: int) -> GovernanceBlockException:
    """Build the depth-exceeded exception (shared by sync and async guards)."""
    return GovernanceBlockException.from_violation(
        GovernanceViolation(
            rule_id="ASI-02",
            rule_name="Excessive Agency",
            detail=f"Delegation depth {current} exceeds max {resolved_max}",
        )
    )


def _wrap_invoke(original: Any, agent_key: int, resolved_max: int) -> Any:
    """Return a depth-guarded wrapper matching the sync/async shape of ``original``.

    Coroutine functions get an ``async def`` wrapper so the returned object
    is itself an awaitable — wrapping with a sync function would return an
    un-awaited coroutine and silently bypass the guard entirely.

    Depth lives in the module-level :data:`_DELEGATION_DEPTHS` ContextVar
    keyed by ``agent_key`` (``id(agent)``), so every guarded agent shares
    the same ContextVar instance and the interpreter's ContextVar
    registry doesn't grow with each install.
    """
    if asyncio.iscoroutinefunction(original):

        @functools.wraps(original)
        async def _guarded_async(input_data: Any, **kwargs: Any) -> Any:
            current, token = _enter_depth_if_under(agent_key, resolved_max)
            if token is None:
                raise _build_violation(current, resolved_max)
            try:
                return await original(input_data, **kwargs)
            finally:
                _exit_depth(token)

        return _guarded_async

    @functools.wraps(original)
    def _guarded_sync(input_data: Any, **kwargs: Any) -> Any:
        current, token = _enter_depth_if_under(agent_key, resolved_max)
        if token is None:
            raise _build_violation(current, resolved_max)
        try:
            return original(input_data, **kwargs)
        finally:
            _exit_depth(token)

    return _guarded_sync


# Method names we guard on the agent. ``ainvoke`` is required because
# LangChain / LangGraph / LlamaIndex agents expose it as the primary
# async entrypoint; wrapping only ``invoke`` would let async callers
# bypass the depth check entirely. A single ContextVar is shared across
# both so an async call that internally falls through to sync ``invoke``
# still increments the same counter.
_GUARDED_METHODS = ("invoke", "ainvoke")


def install_delegation_guard(agent: Any, max_depth: int | None = None) -> None:
    """Patch the agent's invoke methods to enforce a maximum delegation depth.

    Patches both ``invoke`` and ``ainvoke`` when present; each wrapper
    matches the sync/async shape of the original so awaitables stay
    awaitable. No-op when neither attribute exists or the agent has
    already been guarded.

    Per-call-chain depth is tracked in a single :class:`contextvars.ContextVar`
    shared across both methods so an ``ainvoke`` that internally calls
    ``invoke`` still increments the same counter. Concurrent invokes on
    the same agent (across threads or asyncio tasks) keep separate
    counters because ContextVar values are per-context.

    Originals are stashed on the agent under
    ``_uipath_original_<method>`` so :func:`uninstall_delegation_guard`
    can restore them on dispose.
    """
    if max_depth is None:
        max_depth = _resolve_max_depth()
    if getattr(agent, "_delegation_wrapped", False):
        return

    originals = {
        name: getattr(agent, name, None)
        for name in _GUARDED_METHODS
        if callable(getattr(agent, name, None))
    }
    if not originals:
        return

    agent_key = id(agent)
    resolved_max = max_depth

    patched: list[str] = []
    for name, original in originals.items():
        try:
            setattr(agent, name, _wrap_invoke(original, agent_key, resolved_max))
            setattr(agent, f"_uipath_original_{name}", original)
            patched.append(name)
        except (AttributeError, TypeError) as exc:
            # Some agent objects expose `invoke` via __getattr__ or via a
            # slot/descriptor that can't be re-assigned. Skip those —
            # better to guard partial coverage than to crash the runtime.
            logger.debug("Could not patch %s on agent: %s", name, exc)

    if not patched:
        # Nothing was actually wrapped — don't mark the agent as guarded,
        # or a later retry / uninstall would wrongly assume methods were
        # patched.
        logger.debug("Delegation guard patched no methods; leaving agent unguarded")
        return

    agent._delegation_wrapped = True
    logger.debug(
        "Delegation guard installed (max=%d, methods=%s)",
        resolved_max,
        patched,
    )


def uninstall_delegation_guard(agent: Any) -> None:
    """Restore the agent's invoke methods if a delegation guard was installed.

    Safe to call on agents that were never guarded. Also clears the
    agent's entry from the current context's depth map — ``id(agent)``
    is reused by Python after GC, so a stale entry could mis-attribute
    a future agent's count to this one.
    """
    if not getattr(agent, "_delegation_wrapped", False):
        return
    for name in _GUARDED_METHODS:
        attr = f"_uipath_original_{name}"
        original = getattr(agent, attr, None)
        if original is not None:
            try:
                setattr(agent, name, original)
            except Exception as exc:  # noqa: BLE001 - dispose path; never raise
                logger.debug("Could not restore original %s: %s", name, exc)
        try:
            delattr(agent, attr)
        except AttributeError:
            pass
    agent._delegation_wrapped = False
    # Drop the agent's depth entry in the current context. Best-effort
    # — if dispose runs from a different context than where the depth
    # was set, the foreign context still owns its own copy and will
    # discard it when it ends.
    agent_key = id(agent)
    try:
        depths = _DELEGATION_DEPTHS.get()
    except LookupError:
        return
    if agent_key in depths:
        new_depths = {k: v for k, v in depths.items() if k != agent_key}
        _DELEGATION_DEPTHS.set(new_depths)
