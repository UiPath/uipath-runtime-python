"""Audit sink framework for governance events.

This module provides a pluggable audit system that supports multiple
output destinations (sinks) for governance events. Events are emitted
to all registered sinks, allowing flexible audit trail configuration.

Usage::

    from uipath.runtime.governance.audit import get_audit_manager, AuditEvent

    # Get the global audit manager
    manager = get_audit_manager()

    # Emit an event (goes to all registered sinks)
    manager.emit(AuditEvent(
        event_type="rule_evaluation",
        trace_id="abc-123",
        agent_name="my-agent",
        data={"rule_id": "ASI-01", "matched": True},
    ))

    # Register a custom sink
    manager.register_sink(MyCustomSink())

Built-in sinks:

- :class:`TracesAuditSink`  – OpenTelemetry spans for Orchestrator Traces UI
- :class:`ConsoleAuditSink` – stderr output for debugging

Sink registration:

- The ``traces`` sink (OpenTelemetry spans → Orchestrator audit UI) is
  **platform-mandated** and always registered. It cannot be disabled by
  a developer-side env var — governance is platform-owned.
- The ``console`` sink is a developer aid for local debugging and is
  opt-in via env var.

Environment variables (developer-facing, console only):

- ``UIPATH_AUDIT_VERBOSE`` – verbose console output.
- ``UIPATH_GOVERNANCE_CONSOLE_LOG`` – enable the console sink.
"""

from .base import (
    AuditEvent,
    AuditManager,
    AuditSink,
    EventType,
    get_audit_manager,
    reset_audit_manager,
)
from .console import ConsoleAuditSink
from .factory import create_sink
from .traces import TracesAuditSink

__all__ = [
    # Core classes
    "AuditEvent",
    "AuditManager",
    "AuditSink",
    "EventType",
    # Global manager
    "get_audit_manager",
    "reset_audit_manager",
    # Factory
    "create_sink",
    # Built-in sinks
    "ConsoleAuditSink",
    "TracesAuditSink",
]
