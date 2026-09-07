"""Process-global log sink for the pooled path.

In pooled mode the job runs in-process inside ``uipath server``, which owns the CoreIpc
callback to the handler. The runtime can't reach that callback directly, so the pooled
server registers a sink here and the runtime's log handler forwards to it. The sink is
called from the job's logging (a worker thread), so it must be non-blocking and thread-safe.
"""

from __future__ import annotations

from typing import Callable

from uipath.runtime.jobapi.contract import JobLogDto

PooledLogSink = Callable[[str, JobLogDto], None]

_sink: "PooledLogSink | None" = None


def set_pooled_log_sink(sink: "PooledLogSink | None") -> None:
    """Register (or clear, with ``None``) the process-global pooled log sink."""
    global _sink
    _sink = sink


def get_pooled_log_sink() -> "PooledLogSink | None":
    """The registered pooled log sink, or None when not in a pooled server."""
    return _sink
