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
    """Undo a successful :func:`_enter_depth_if_under` call."""
    try:
        _DELEGATION_DEPTHS.reset(token)
    except (ValueError, LookupError):
        logger.debug("Delegation depth reset from foreign context")


def _resolve_max_depth() -> int:
    """Read max-depth from env at call time, falling back to default on parse error."""
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
    """Return a depth-guarded wrapper matching the sync/async shape of ``original``."""
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


_GUARDED_METHODS = ("invoke", "ainvoke")


def install_delegation_guard(agent: Any, max_depth: int | None = None) -> None:
    """Patch the agent's invoke methods to enforce a maximum delegation depth.

    Patches both ``invoke`` and ``ainvoke`` when present. No-op when
    neither attribute exists or the agent has already been guarded.
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

    for name, original in originals.items():
        try:
            setattr(agent, name, _wrap_invoke(original, agent_key, resolved_max))
            setattr(agent, f"_uipath_original_{name}", original)
        except (AttributeError, TypeError) as exc:
            logger.debug("Could not patch %s on agent: %s", name, exc)
    agent._delegation_wrapped = True
    logger.debug(
        "Delegation guard installed (max=%d, methods=%s)",
        resolved_max,
        list(originals),
    )


def uninstall_delegation_guard(agent: Any) -> None:
    """Restore the agent's invoke methods if a delegation guard was installed.

    Safe to call on agents that were never guarded.
    """
    if not getattr(agent, "_delegation_wrapped", False):
        return
    for name in _GUARDED_METHODS:
        attr = f"_uipath_original_{name}"
        original = getattr(agent, attr, None)
        if original is not None:
            try:
                setattr(agent, name, original)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Could not restore original %s: %s", name, exc)
        try:
            delattr(agent, attr)
        except AttributeError:
            pass
    agent._delegation_wrapped = False
    agent_key = id(agent)
    try:
        depths = _DELEGATION_DEPTHS.get()
    except LookupError:
        return
    if agent_key in depths:
        new_depths = {k: v for k, v in depths.items() if k != agent_key}
        _DELEGATION_DEPTHS.set(new_depths)
