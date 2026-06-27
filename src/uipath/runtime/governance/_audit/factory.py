"""Factory function for creating audit sinks by name.

Used by :class:`AuditManager` to construct the always-on ``traces``
sink at initialization.
"""

from __future__ import annotations

import logging

from .base import AuditSink

logger = logging.getLogger(__name__)


def create_sink(name: str) -> AuditSink | None:
    """Create an audit sink by name.

    Args:
        name: Name of the sink to create (currently only ``traces``).

    Returns:
        The created sink, or ``None`` if the name is unknown.
    """
    name = name.lower()

    if name == "traces":
        from .traces import TracesAuditSink

        return TracesAuditSink()

    logger.warning("Unknown audit sink: %s", name)
    return None
