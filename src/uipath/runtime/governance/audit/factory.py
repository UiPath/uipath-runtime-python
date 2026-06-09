"""Factory function for creating audit sinks by name.

This module provides the create_sink function used by the AuditManager
to instantiate sinks based on environment configuration.
"""

from __future__ import annotations

import logging
import os

from .base import AuditSink

logger = logging.getLogger(__name__)


def create_sink(name: str) -> AuditSink | None:
    """Create an audit sink by name.

    Args:
        name: Name of the sink to create (``traces`` or ``console``).

    Returns:
        The created sink, or ``None`` if the name is unknown.

    Supported sinks:
        - ``traces``: OpenTelemetry spans for Orchestrator Traces UI
        - ``console``: human-readable stderr output
    """
    name = name.lower()

    if name == "traces":
        from .traces import TracesAuditSink

        return TracesAuditSink()

    elif name == "console":
        from .console import ConsoleAuditSink

        verbose = os.getenv("UIPATH_AUDIT_VERBOSE", "false").lower() == "true"
        return ConsoleAuditSink(verbose=verbose)

    else:
        logger.warning("Unknown audit sink: %s", name)
        return None
