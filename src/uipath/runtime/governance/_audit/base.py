"""Base classes and models for the audit sink framework.

This module provides the core abstractions for the governance audit system:
- AuditEvent: The data model for audit events
- EventType: Constants for common event types
- AuditSink: Abstract base class for sink implementations
- AuditManager: Central hub for routing events to sinks

Sink dispatch is synchronous on the caller's thread. Sinks that need
async export (HTTP, batched I/O) own that concern internally — the
OTel traces sink rides on opentelemetry-sdk's BatchSpanProcessor,
which handles export off the caller's thread.
"""

from __future__ import annotations

import json
import logging
import threading
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from uipath.core.governance import EnforcementMode

logger = logging.getLogger(__name__)


# =============================================================================
# Audit Event Model
# =============================================================================


@dataclass
class AuditEvent:
    """Generic audit event that can be sent to any sink.

    Trace correlation is intentionally absent from this dataclass.
    Sinks that need a trace id resolve one at their own boundary:
    OTel-backed sinks read the live span from the caller's
    ``contextvars`` directly (sink dispatch runs synchronously on the
    caller's thread, so ``trace.get_current_span()`` resolves to the
    agent's live span), and HTTP sinks defer to their injected
    provider, which resolves at HTTP-call time.

    Attributes:
        event_type: Type of event (e.g., "rule_evaluation", "hook_summary")
        timestamp: When the event occurred (auto-set if not provided)
        agent_name: Name of the agent being governed
        hook: Lifecycle hook where event occurred (optional)
        data: Event-specific data dictionary
        metadata: Additional metadata for filtering/routing
    """

    event_type: str
    agent_name: str = "unknown"
    hook: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        result = asdict(self)
        result["timestamp"] = self.timestamp.isoformat()
        return result

    def to_json(self) -> str:
        """Convert to JSON string."""
        return json.dumps(self.to_dict())


class EventType:
    """Constants for common event types."""

    RULE_EVALUATION = "rule_evaluation"
    HOOK_START = "hook_start"
    HOOK_END = "hook_end"
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    POLICY_VIOLATION = "policy_violation"
    POLICY_ALLOW = "policy_allow"
    PACKS_LOADED = "packs_loaded"


# =============================================================================
# Audit Sink Base Class
# =============================================================================


class AuditSink(ABC):
    """Abstract base class for audit output destinations.

    Subclass this to create custom audit sinks. Each sink receives
    all audit events and decides how to handle them.

    Sinks that perform network I/O should batch internally — :meth:`emit`
    runs on the caller's thread (typically an agent hook), so a slow
    synchronous sink blocks the agent. The standard pattern is the one
    opentelemetry-sdk uses for its trace exporter: enqueue in-process,
    drain on a sink-owned background thread.

    Example:
        A Slack sink that posts on rule denials. ``emit`` enqueues onto
        an in-process queue; a daemon thread the sink owns drains the
        queue and runs the HTTP POST off the caller's thread.

        class SlackAuditSink(AuditSink):
            def __init__(self, webhook_url: str):
                self.webhook_url = webhook_url
                self._name = "slack"
                self._queue: queue.Queue[AuditEvent | None] = queue.Queue()
                self._worker = threading.Thread(
                    target=self._drain, name="slack-audit", daemon=True
                )
                self._worker.start()

            @property
            def name(self) -> str:
                return self._name

            def accepts(self, event: AuditEvent) -> bool:
                # Only ship denials — drops irrelevant events at the
                # boundary instead of forwarding them to the queue.
                return (
                    event.data.get("matched")
                    and event.data.get("action") == "deny"
                )

            def emit(self, event: AuditEvent) -> None:
                # Non-blocking — runs on the caller's hook thread.
                self._queue.put_nowait(event)

            def _drain(self) -> None:
                while True:
                    event = self._queue.get()
                    if event is None:
                        return  # close() sentinel
                    try:
                        requests.post(self.webhook_url, json=event.to_dict())
                    except Exception:
                        pass  # log/retry per sink's own policy
                    finally:
                        self._queue.task_done()

            def flush(self) -> None:
                self._queue.join()

            def close(self) -> None:
                self._queue.put_nowait(None)
                self._worker.join(timeout=2.0)
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique name for this sink."""
        pass

    @abstractmethod
    def emit(self, event: AuditEvent) -> None:
        """Emit an audit event to this sink.

        Args:
            event: The audit event to emit

        Note:
            Implementations should handle errors gracefully and not
            raise exceptions that would disrupt governance evaluation.
        """
        pass

    def flush(self) -> None:
        """Flush any buffered events.

        Override if sink buffers events before writing.
        """
        return

    def close(self) -> None:
        """Clean up resources.

        Override if sink holds resources that need cleanup.
        """
        return

    def accepts(self, event: AuditEvent) -> bool:
        """Check if this sink should receive the event.

        Override to filter events. Default accepts all events.

        Args:
            event: The audit event to check

        Returns:
            True if sink should receive event, False to skip
        """
        return True


# =============================================================================
# Audit Manager
# =============================================================================


class AuditManager:
    """Manages multiple audit sinks and routes events to them.

    Instance-scoped: each :class:`GovernanceRuntime` owns its own
    manager. Parallel runtimes (``uipath eval``) don't share sinks or
    per-sink failure state.

    Constructor automatically registers the always-on ``traces`` sink,
    which carries the governance audit trail and cannot be disabled by
    application code. Additional sinks can be added via
    :meth:`register_sink`.

    Thread Safety:
        :meth:`emit` dispatches synchronously on the caller's thread.
        Sinks that need to avoid blocking the caller (HTTP exporters)
        own their own batching — the OTel traces sink, for example,
        rides on opentelemetry-sdk's BatchSpanProcessor.
    """

    # Trip a sink after this many consecutive emit failures (circuit-breaker).
    _SINK_FAILURE_THRESHOLD = 10

    def __init__(self, register_default_sinks: bool = True) -> None:
        """Initialize the audit manager.

        Args:
            register_default_sinks: If True (default), register the
                always-on ``traces`` sink. Tests that want a bare
                manager can pass ``False`` and register sinks
                explicitly.
        """
        self._sinks: list[AuditSink] = []
        # Guards _sinks, _sink_failures, _tripped_sinks — all read +
        # mutated by emit() across threads when concurrent agent hooks
        # share one manager.
        self._sinks_lock = threading.Lock()
        # Per-sink consecutive-failure counter, keyed by sink name.
        self._sink_failures: dict[str, int] = {}
        self._tripped_sinks: set[str] = set()

        if register_default_sinks:
            self._register_traces_sink()

    def _register_traces_sink(self) -> None:
        """Register the always-on ``traces`` sink.

        Registered for every manager and cannot be disabled by
        application code — it carries the governance audit trail. The
        factory import is deferred to avoid a module-load cycle
        (``factory`` imports back into this module).
        """
        from .factory import create_sink

        sink = create_sink("traces")
        if sink is not None:
            self.register_sink(sink)
            logger.info("Governance audit sink registered: traces")

    def register_sink(self, sink: AuditSink) -> None:
        """Register an audit sink.

        Args:
            sink: The sink to register

        Note:
            Duplicate sinks (same name) are ignored.
            The circuit-breaker failure counter is cleared so a freshly
            registered sink doesn't inherit a previous instance's tripped
            state. ``unregister_sink`` already clears these, but the
            defensive reset here guards against external manipulation
            of the internal counters (tests, future callers).
        """
        with self._sinks_lock:
            if any(s.name == sink.name for s in self._sinks):
                logger.debug("Sink '%s' already registered, skipping", sink.name)
                return
            self._sinks.append(sink)
            self._sink_failures.pop(sink.name, None)
            self._tripped_sinks.discard(sink.name)
        logger.info("Registered audit sink: %s", sink.name)

    def unregister_sink(self, name: str) -> bool:
        """Unregister an audit sink by name.

        Args:
            name: Name of the sink to remove

        Returns:
            True if sink was removed, False if not found
        """
        sink_to_close: AuditSink | None = None
        with self._sinks_lock:
            for i, sink in enumerate(self._sinks):
                if sink.name == name:
                    sink_to_close = sink
                    del self._sinks[i]
                    self._sink_failures.pop(name, None)
                    self._tripped_sinks.discard(name)
                    break
        if sink_to_close is not None:
            try:
                sink_to_close.close()
            except Exception as e:
                logger.warning("Audit sink '%s' failed to close: %s", name, e)
            logger.info("Unregistered audit sink: %s", name)
            return True
        return False

    def get_sink(self, name: str) -> AuditSink | None:
        """Get a registered sink by name."""
        with self._sinks_lock:
            for sink in self._sinks:
                if sink.name == name:
                    return sink
        return None

    def list_sinks(self) -> list[str]:
        """Get names of all registered sinks."""
        with self._sinks_lock:
            return [s.name for s in self._sinks]

    def emit(self, event: AuditEvent) -> None:
        """Dispatch ``event`` synchronously to every live sink.

        Per-sink errors are caught and folded into the circuit breaker
        — a sink that fails too many times in a row is skipped for the
        rest of the manager's lifetime. The caller never sees a sink
        exception.

        Args:
            event: The audit event to emit
        """
        with self._sinks_lock:
            sinks = list(self._sinks)
            tripped = set(self._tripped_sinks)
        for sink in sinks:
            if sink.name in tripped:
                continue
            try:
                if sink.accepts(event):
                    sink.emit(event)
                # Success — reset failure counter for this sink.
                with self._sinks_lock:
                    if self._sink_failures.get(sink.name):
                        self._sink_failures[sink.name] = 0
            except Exception as e:
                with self._sinks_lock:
                    fails = self._sink_failures.get(sink.name, 0) + 1
                    self._sink_failures[sink.name] = fails
                    tripped_now = fails >= self._SINK_FAILURE_THRESHOLD
                    if tripped_now:
                        self._tripped_sinks.add(sink.name)
                if tripped_now:
                    logger.error(
                        "Audit sink '%s' tripped after %d consecutive failures; "
                        "will be skipped for the rest of this process. Last error: %s",
                        sink.name,
                        fails,
                        e,
                    )
                else:
                    logger.warning(
                        "Audit sink '%s' failed to emit event (%d/%d): %s",
                        sink.name,
                        fails,
                        self._SINK_FAILURE_THRESHOLD,
                        e,
                    )

    def emit_rule_evaluation(
        self,
        policy_id: str,
        rule_name: str,
        pack_name: str,
        hook: str,
        matched: bool,
        action: str,
        enforcement_mode: EnforcementMode,
        detail: str = "",
        agent_name: str = "agent",
        description: str = "",
    ) -> None:
        """Convenience method to emit a rule evaluation event.

        ``enforcement_mode`` travels on the event so sinks don't have to
        read a process-global. Each emitter (instance-scoped) supplies
        its own mode — parallel runtimes can run in different modes
        simultaneously, and a process-global wouldn't be authoritative
        for any of them.
        """
        self.emit(
            AuditEvent(
                event_type=EventType.RULE_EVALUATION,
                agent_name=agent_name,
                hook=hook,
                data={
                    "policy_id": policy_id,
                    "rule_name": rule_name,
                    "pack_name": pack_name,
                    "matched": matched,
                    "action": action,
                    "enforcement_mode": enforcement_mode,
                    "detail": detail,
                    "description": description,
                    "status": "MATCHED" if matched else "PASS",
                },
            )
        )

    def emit_hook_summary(
        self,
        hook: str,
        agent_name: str,
        total_rules: int,
        matched_rules: int,
        final_action: str,
        enforcement_mode: EnforcementMode,
    ) -> None:
        """Convenience method to emit a hook summary event."""
        self.emit(
            AuditEvent(
                event_type=EventType.HOOK_END,
                agent_name=agent_name,
                hook=hook,
                data={
                    "total_rules": total_rules,
                    "matched_rules": matched_rules,
                    "final_action": final_action,
                    "enforcement_mode": enforcement_mode,
                },
            )
        )

    def emit_session_start(
        self,
        session_id: str,
        agent_name: str,
        packs: list[str],
        enforcement_mode: EnforcementMode,
    ) -> None:
        """Convenience method to emit a session start event.

        Same ``enforcement_mode: EnforcementMode`` contract as
        :meth:`emit_rule_evaluation` and :meth:`emit_hook_summary`
        — every governance event carries the emitter's per-instance
        mode so sinks don't depend on a process-global.
        """
        self.emit(
            AuditEvent(
                event_type=EventType.SESSION_START,
                agent_name=agent_name,
                data={
                    "session_id": session_id,
                    "packs": packs,
                    "enforcement_mode": enforcement_mode,
                },
            )
        )

    def emit_session_end(
        self,
        session_id: str,
        agent_name: str,
        total_evaluations: int,
        rules_matched: int,
        rules_denied: int,
        enforcement_mode: EnforcementMode,
    ) -> None:
        """Convenience method to emit a session end event."""
        self.emit(
            AuditEvent(
                event_type=EventType.SESSION_END,
                agent_name=agent_name,
                data={
                    "session_id": session_id,
                    "total_evaluations": total_evaluations,
                    "rules_matched": rules_matched,
                    "rules_denied": rules_denied,
                    "enforcement_mode": enforcement_mode,
                },
            )
        )

    def flush(self) -> None:
        """Flush every registered sink.

        Per-sink — a sink that maintains its own buffer (OTel batched
        export, HTTP batcher, etc.) gets a chance to drain. The
        manager itself holds no queue.
        """
        with self._sinks_lock:
            sinks = list(self._sinks)
        for sink in sinks:
            try:
                sink.flush()
            except Exception as e:
                logger.warning("Audit sink '%s' failed to flush: %s", sink.name, e)

    def close(self) -> None:
        """Close all sinks and release resources.

        Idempotent — a manager that has already been closed has an
        empty sink list, so a repeat call is a no-op.
        """
        with self._sinks_lock:
            sinks = list(self._sinks)
            self._sinks.clear()
            self._sink_failures.clear()
            self._tripped_sinks.clear()
        for sink in sinks:
            try:
                sink.close()
            except Exception as e:
                logger.warning("Audit sink '%s' failed to close: %s", sink.name, e)
