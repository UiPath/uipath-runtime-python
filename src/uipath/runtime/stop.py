"""Per-execution slot through which a host says whether it asked the execution to stop.

Unset, a cancelled execution ends like any other failure. Set, a cancellation the host asked for
ends the execution as stopped. A contextvar, so concurrent jobs in one process each see their own
host's answer.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Callable

# () -> whether the host has asked this execution to stop
StopRequestedProbe = Callable[[], bool]

_stop_requested_probe: ContextVar[StopRequestedProbe | None] = ContextVar(
    "uipath_stop_requested_probe", default=None
)


def set_stop_requested_probe(probe: StopRequestedProbe | None) -> None:
    """Install the probe (``None`` clears)."""
    _stop_requested_probe.set(probe)


def get_stop_requested_probe() -> StopRequestedProbe | None:
    """The installed probe, or None."""
    return _stop_requested_probe.get()
