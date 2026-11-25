"""Per-execution slot through which a host says whether it stopped the execution.

A cancelled execution ends as stopped only when the host's probe confirms the stop: a
CancelledError on its own cannot tell a stop from a timeout or a TaskGroup tearing down.
Unset, a cancelled execution ends like any other failure. A contextvar, so concurrent
executions in one process each see their own host's answer.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Callable

# () -> whether the host has stopped this execution
StopRequestedProbe = Callable[[], bool]

_stop_requested_probe: ContextVar[StopRequestedProbe | None] = ContextVar(
    "uipath_stop_requested_probe", default=None
)


def set_stop_requested_probe(probe: StopRequestedProbe | None) -> None:
    """Install the probe for the current context (``None`` clears)."""
    _stop_requested_probe.set(probe)


def get_stop_requested_probe() -> StopRequestedProbe | None:
    """The installed probe, or None."""
    return _stop_requested_probe.get()
