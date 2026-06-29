"""Base classes and models for the audit sink framework.

This module provides the core abstractions for the governance audit system:
- AuditEvent: The data model for audit events
- EventType: Constants for common event types
- AuditSink: Abstract base class for sink implementations
- AuditManager: Central hub for routing events to sinks

The AuditManager uses a background thread to process events asynchronously,
avoiding blocking the main agent execution path during audit trace HTTP calls.
"""

from __future__ import annotations

import atexit
import contextvars
import json
import logging
import os
import queue
import threading
import weakref
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from uipath.core.governance import EnforcementMode

logger = logging.getLogger(__name__)


class _AuditManagerCleanupRegistry:
    """Process-wide cleanup machinery for :class:`AuditManager` instances.

    A single ``atexit`` hook walks a ``WeakSet`` of live managers on
    exit and flushes/closes each one. Two important properties:

    1. **Bounded atexit registrations.** Per-instance ``atexit.register``
       grows the interpreter's atexit list without bound — N runtimes
       → N hooks → N × shutdown-timeout total exit delay. One
       process-level hook is constant work regardless of how many
       managers were constructed.

    2. **No strong reference to the manager.** ``WeakSet`` lets a
       disposed manager get garbage-collected; if it's already gone by
       exit time, we just skip it. Long-running ``uipath eval`` runs
       that build many runtimes serially can therefore release each
       one's memory as soon as nothing references it, instead of
       pinning all of them until process exit.

    Encapsulated in a class (rather than three loose module-level
    names + a ``global`` mutation) so the state is named, swappable in
    tests, and the registration path doesn't reach across the module
    scope to assign.
    """

    def __init__(self) -> None:
        self.live_managers: weakref.WeakSet[AuditManager] = weakref.WeakSet()
        self.atexit_registered = False
        self.lock = threading.Lock()

    def register(self, manager: AuditManager) -> None:
        """Add ``manager`` to the cleanup set + wire process atexit once.

        Double-checked under ``lock`` so two concurrent first-time
        constructions don't both register the process atexit handler.
        """
        self.live_managers.add(manager)
        if self.atexit_registered:
            return
        with self.lock:
            if not self.atexit_registered:
                atexit.register(self.process_cleanup)
                self.atexit_registered = True

    def process_cleanup(self) -> None:
        """Process-exit handler: flush + close every live AuditManager.

        Iteration over a snapshot — the WeakSet may mutate during
        cleanup (close() touches sinks_lock, GC may fire). Bounded by
        each manager's own flush / close timeouts.
        """
        for manager in list(self.live_managers):
            try:
                manager.flush(timeout=2.0)
                manager.close()
            except Exception as exc:  # noqa: BLE001 - exit cleanup must not raise
                logger.debug("Audit manager process cleanup error: %s", exc)


_cleanup_registry = _AuditManagerCleanupRegistry()


# =============================================================================
# Audit Event Model
# =============================================================================


@dataclass
class AuditEvent:
    """Generic audit event that can be sent to any sink.

    Trace correlation is intentionally absent from this dataclass.
    Sinks that need a trace id resolve one at their own boundary:
    OTel-backed sinks let the SDK / exporter handle it (the audit
    manager runs sink dispatch inside the caller's captured
    contextvars snapshot, so the live OTel span is visible on the
    worker), and HTTP sinks defer to their injected provider, which
    resolves at HTTP-call time.

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

    Example:
        class SlackAuditSink(AuditSink):
            def __init__(self, webhook_url: str):
                self.webhook_url = webhook_url
                self._name = "slack"

            @property
            def name(self) -> str:
                return self._name

            def emit(self, event: AuditEvent) -> None:
                if event.data.get("matched") and event.data.get("action") == "deny":
                    # Send to Slack on violations
                    requests.post(self.webhook_url, json=event.to_dict())

            def flush(self) -> None:
                pass
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
    manager. Parallel runtimes (``uipath eval``) don't share sinks,
    workers, or per-sink failure state.

    Constructor automatically registers the always-on ``traces`` sink,
    which carries the governance audit trail and cannot be disabled by
    application code. Additional sinks can be added via
    :meth:`register_sink`.

    Thread Safety:
        Events are queued and processed by a background thread, making
        :meth:`emit` non-blocking. This avoids blocking agent execution
        while a sink is doing I/O.
    """

    # Trip a sink after this many consecutive emit failures (circuit-breaker).
    _SINK_FAILURE_THRESHOLD = 10
    # Bound the async queue so a stuck sink can't grow memory without limit.
    # Matches the order of magnitude of a long-running agent's per-session
    # audit volume; on overflow the oldest event is dropped to make room.
    _DEFAULT_QUEUE_MAXSIZE = 10_000

    def __init__(
        self,
        async_mode: bool = True,
        queue_maxsize: int = _DEFAULT_QUEUE_MAXSIZE,
        register_default_sinks: bool = True,
    ) -> None:
        """Initialize the audit manager.

        Args:
            async_mode: If True (default), events are processed in a background
                       thread. If False, events are processed synchronously.
            queue_maxsize: Max queued events in async mode. On overflow the
                       oldest queued event is dropped to make room.
            register_default_sinks: If True (default), register the
                       always-on ``traces`` sink and an atexit cleanup
                       handler. Tests that want a bare manager can pass
                       ``False`` and register sinks explicitly.
        """
        self._sinks: list[AuditSink] = []
        # Single lock guards _sinks, _sink_failures, _tripped_sinks — every
        # collection mutated by both the worker thread and the emit caller.
        self._sinks_lock = threading.Lock()
        # Per-sink consecutive-failure counter, keyed by sink name.
        self._sink_failures: dict[str, int] = {}
        self._tripped_sinks: set[str] = set()
        self._async_mode = async_mode
        self._pid = os.getpid()

        # Background processing.
        #
        # Queue items are ``(contextvars.Context, AuditEvent)`` tuples
        # so the caller's contextvars context (which holds the live
        # OTel span, request correlation ids, etc.) propagates across
        # the worker-thread hop. Without this the worker would see an
        # empty contextvars context — OTel-backed sinks would render
        # governance spans as orphan roots instead of children of the
        # agent's live span. ``None`` is the shutdown sentinel.
        self._queue: queue.Queue[
            tuple[contextvars.Context, AuditEvent] | None
        ] = queue.Queue(maxsize=queue_maxsize)
        self._worker_thread: threading.Thread | None = None
        self._shutdown = threading.Event()

        if self._async_mode:
            self._start_worker()

        if register_default_sinks:
            self._register_traces_sink()
            # Process-level atexit (one shared handler, weakref-tracked
            # set) instead of per-instance ``atexit.register(self.method)``:
            # avoids unbounded atexit list growth and the strong reference
            # that would otherwise pin a disposed manager until process
            # exit. See :class:`_AuditManagerCleanupRegistry`.
            _cleanup_registry.register(self)

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

    def _start_worker(self) -> None:
        """Start the background worker thread."""
        if self._worker_thread is not None and self._worker_thread.is_alive():
            return

        self._shutdown.clear()
        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            name="governance-audit-worker",
            daemon=True,
        )
        self._worker_thread.start()
        logger.debug("Background audit worker started")

    def _worker_loop(self) -> None:
        """Background worker loop that processes queued events."""
        while not self._shutdown.is_set():
            # Wait for an item with a timeout so we can re-check shutdown.
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            # Every successful get() must be paired with exactly one
            # task_done() — including the shutdown sentinel and the case
            # where _emit_sync raises — otherwise unfinished_tasks never
            # drains and flush()/join() hangs.
            try:
                if item is None:
                    # Shutdown signal
                    break
                ctx, event = item
                # Run sink dispatch inside the caller's captured
                # contextvars context so OTel-backed sinks see the
                # agent's live span via ``context.get_current()``.
                ctx.run(self._emit_sync, event)
            except Exception as e:
                logger.warning("Audit worker error: %s", e)
            finally:
                self._queue.task_done()

        # Drain remaining events on shutdown
        self._drain_queue()

    def _drain_queue(self) -> None:
        """Process any remaining events in the queue."""
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            # As in _worker_loop: pair every get() with one task_done(),
            # even when _emit_sync raises, so shutdown accounting is sound.
            try:
                if item is not None:
                    ctx, event = item
                    ctx.run(self._emit_sync, event)
            except Exception as e:
                logger.warning("Audit drain error: %s", e)
            finally:
                self._queue.task_done()

    def _emit_sync(self, event: AuditEvent) -> None:
        """Emit event synchronously to all sinks (called from worker thread)."""
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
        """Emit an audit event to all registered sinks.

        In async mode (default), this queues the event for background
        processing and returns immediately. This avoids blocking the
        main agent execution path during audit trace HTTP calls.

        On post-fork callers (worker process inheriting the parent's
        manager), the queue is reinitialized and the worker thread
        re-spawned before enqueue — otherwise events would silently
        accumulate in a queue no one is draining.

        Args:
            event: The audit event to emit
        """
        self._ensure_alive_after_fork()

        if self._async_mode:
            # Capture the caller's contextvars context now (while the
            # OTel span and request correlation state are still live
            # on this thread). The worker runs the sink dispatch
            # inside this snapshot so cross-thread sinks see the same
            # context the caller had. See queue type in __init__.
            item = (contextvars.copy_context(), event)
            # Non-blocking enqueue with drop-oldest backpressure: if the
            # worker is wedged on a slow sink, this keeps memory bounded
            # rather than growing without limit.
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                try:
                    self._queue.get_nowait()
                    self._queue.task_done()
                except queue.Empty:
                    pass
                try:
                    self._queue.put_nowait(item)
                except queue.Full:
                    # Worker is so far behind that the queue refilled
                    # between get_nowait and put_nowait — give up on
                    # this event rather than block.
                    pass
        else:
            # Synchronous processing — caller thread IS the worker, so
            # the OTel context is already correct; no context snapshot
            # or ctx.run() needed.
            self._emit_sync(event)

    def _ensure_alive_after_fork(self) -> None:
        """Reset queue and respawn worker if we're in a forked child.

        Double-checked under ``_sinks_lock``: a fresh-fork child where
        multiple threads call :meth:`emit` concurrently could otherwise
        each see the stale ``_pid`` and each rebuild ``_queue`` /
        ``_shutdown`` / ``_worker_thread`` — one thread's writes would
        clobber the other's, leaking the queue+worker pair.
        """
        if os.getpid() == self._pid:
            return  # fast path: same process, no rebuild needed
        with self._sinks_lock:
            current_pid = os.getpid()
            if current_pid == self._pid:
                return  # another thread won the rebuild race
            # Child process inherited a dead worker_thread reference and
            # a queue the parent owned. Rebuild both so child events drain.
            self._pid = current_pid
            self._queue = queue.Queue(maxsize=self._queue.maxsize)
            self._shutdown = threading.Event()
            self._worker_thread = None
            if self._async_mode:
                self._start_worker()

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

    def flush(self, timeout: float = 5.0) -> None:
        """Flush all pending events and sinks.

        In async mode, polls the queue until it drains or ``timeout``
        seconds elapse, whichever comes first. ``queue.Queue.join`` has
        no timeout argument — using it would block indefinitely on a
        wedged sink, which defeats the bounded-shutdown contract that
        :class:`_AuditManagerCleanupRegistry` relies on at process exit.

        Args:
            timeout: Maximum seconds to wait for queue to drain (default 5.0)
        """
        if self._async_mode:
            import time

            deadline = time.monotonic() + max(0.0, timeout)
            poll_interval = min(0.05, timeout) if timeout > 0 else 0.0
            while time.monotonic() < deadline:
                try:
                    if self._queue.unfinished_tasks == 0:
                        break
                except Exception:  # noqa: BLE001 - queue introspection is best-effort
                    break
                time.sleep(poll_interval)
            else:
                # Loop didn't break — drain timed out. Log so a wedged
                # sink is surfaced rather than swallowed.
                try:
                    pending = self._queue.unfinished_tasks
                except Exception:  # noqa: BLE001
                    pending = -1
                if pending:
                    logger.warning(
                        "Audit queue did not drain within %.2fs "
                        "(unfinished tasks=%s); sink may be wedged",
                        timeout, pending,
                    )

        with self._sinks_lock:
            sinks = list(self._sinks)
        for sink in sinks:
            try:
                sink.flush()
            except Exception as e:
                logger.warning("Audit sink '%s' failed to flush: %s", sink.name, e)

    def close(self) -> None:
        """Close all sinks and release resources.

        Stops the background worker thread and drains any remaining events.
        Shutdown is bounded: ``_shutdown`` is the primary signal the
        worker polls; the sentinel ``None`` enqueue is best-effort. If
        the queue is full and the worker is wedged on a slow sink,
        ``put_nowait`` fails fast rather than hanging process exit.
        """
        if self._async_mode and self._worker_thread is not None:
            # Signal shutdown first so the worker's next queue.get() loop
            # iteration exits even if we can't enqueue the sentinel.
            self._shutdown.set()
            try:
                self._queue.put_nowait(None)  # Wake up worker
            except queue.Full:
                # Queue saturated by a stuck sink; the worker will see
                # _shutdown on its next loop iteration once whatever it's
                # blocked on completes (or the 2s join timeout fires).
                logger.debug(
                    "Audit queue full at shutdown; relying on _shutdown signal"
                )

            # Wait for worker to finish (with timeout)
            if self._worker_thread.is_alive():
                self._worker_thread.join(timeout=2.0)

            logger.debug("Background audit worker stopped")

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


