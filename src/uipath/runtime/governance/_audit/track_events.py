"""Telemetry sink for governance evaluation events.

Submits custom telemetry events through a caller-supplied **non-blocking**
``track_event`` callable. The runtime layer does not own dispatch — the
host is responsible for supplying a callable that returns immediately
(typically a thread-pool-backed adapter around the real telemetry
client). Tests pass a mock that captures payloads.

Event-rate policy (volume control):

- DENIED rules (``matched=True``) → one ``governance.rule.denied`` event
  per matched rule.
- PASSED + SKIPPED rules → one ``governance.hook.summary`` event per
  hook with counts + the names of skipped rules + the count of
  UiPath-mapped guardrails dispatched through the compensating path.

Why: a 50-rule pack evaluated on every hook of every agent step would
multiply per-step telemetry calls by 50. Bundling the "nothing
happened" cases into a single hook summary cuts that to 1.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from uipath.core.governance import EnforcementMode

from .base import AuditEvent, AuditSink, EventType
from .metadata import GovernanceRuntimeMetadata

logger = logging.getLogger(__name__)


TrackEventCallable = Callable[..., None]
"""Host-supplied telemetry callable.

Expected kwargs: ``event_name: str``, ``data: dict | None``,
``operation_id: str | None``. The sink calls this synchronously from
the caller's hook thread, so the callable **MUST** be non-blocking —
the agent run shares that thread, and any wait propagates straight
into latency the agent observes. Hosts that need to do network I/O
are expected to wrap the underlying call in a thread-pool-backed
adapter that submits the work and returns immediately.
"""


EVENT_RULE_DENIED = "governance.rule.denied"
EVENT_HOOK_SUMMARY = "governance.hook.summary"


def _resolve_operation_id() -> str | None:
    """Read the current OTel span's trace id for downstream correlation.

    Sink dispatch runs on the caller's thread (see
    :meth:`AuditManager.emit`), so the live agent span is visible via
    ``trace.get_current_span()`` directly. Rendered as a 32-char
    lowercase hex string for use as a correlation id by the consumer.

    Returns ``None`` when:

    - OpenTelemetry isn't installed (sink no-ops gracefully).
    - No span is recording (no trace context to correlate against).

    The live span is the single source of truth for trace correlation;
    the sink does not consult environment variables.
    """
    try:
        from opentelemetry import trace
    except ImportError:
        return None

    span = trace.get_current_span()
    span_ctx = span.get_span_context()
    if not span_ctx.is_valid:
        return None
    return f"{span_ctx.trace_id:032x}"


def _mode_str(mode: Any) -> str:
    """Coerce an enforcement-mode field to its canonical uppercase string."""
    if isinstance(mode, EnforcementMode):
        return mode.value.upper()
    if isinstance(mode, str):
        return mode.upper()
    return "AUDIT"


def _evaluator_result(action: str) -> str:
    """Map a rule's configured action to the spec-vocabulary verdict.

    Mirrors :func:`uipath.runtime.governance._audit.traces._derive_results`
    so both sinks agree on the (matched, action) → verdict mapping.
    """
    action_lc = action.lower()
    if action_lc == "deny":
        return "DENY"
    if action_lc == "escalate":
        return "HITL"
    if action_lc == "audit":
        # The rule wanted to deny but was tagged audit-only at the
        # check level — the evaluator's intent is still "deny".
        return "DENY"
    return "ALLOW"


def _action_applied(evaluator_result: str, configured_action: str, mode: str) -> str:
    """Mode-adjusted action: AUDIT mode collapses DENY/HITL into AUDIT."""
    if mode == "AUDIT":
        if evaluator_result in ("DENY", "HITL"):
            return "AUDIT"
        return "NONE"
    # ENFORCE mode: per-rule ``audit`` override stays AUDIT.
    if configured_action.lower() == "audit":
        return "AUDIT"
    return evaluator_result if evaluator_result != "ALLOW" else "NONE"


class TrackEventAuditSink(AuditSink):
    """Sink that emits governance telemetry via the injected ``track_event``.

    Volume-controlled per the module docstring: individual events only
    for denials, one aggregated event per hook for passed / skipped /
    dispatched. Other event types are dropped at :meth:`accepts`.

    The ``track_event`` callable is invoked synchronously on the
    caller's hook thread, so the callable must be non-blocking — see
    the :class:`TrackEventCallable` type alias for the contract the
    host is expected to satisfy.
    """

    SINK_NAME = "track_events"

    def __init__(
        self,
        track_event: TrackEventCallable,
        runtime_metadata: GovernanceRuntimeMetadata,
        *,
        name: str = SINK_NAME,
    ) -> None:
        """Initialize the sink.

        Args:
            track_event: Host-supplied **non-blocking** telemetry
                callable. Receives ``event_name``, ``data``,
                ``operation_id`` kwargs. The sink invokes it
                synchronously from the agent's hook thread, so any
                network or otherwise blocking work in the underlying
                implementation must be offloaded by the host (e.g.,
                via a thread-pool-backed adapter). Tests pass a
                capture callable. **Required** — ``None`` is a wiring
                bug and is rejected here so the manager's registration
                try/except surfaces it as a clear warning rather than
                letting a ``TypeError`` fire on the first event.
            runtime_metadata: Per-run constants stamped on every event
                (execution engine, agent type, agent framework, runtime
                version).
            name: Sink name used for circuit-breaker accounting in the
                manager. Override only if more than one telemetry sink
                of this kind is registered against the same manager.
        """
        if track_event is None:
            raise ValueError(
                "TrackEventAuditSink requires a non-None track_event callable. "
                "The host is expected to supply a non-blocking adapter — "
                "wiring a blocking client (e.g., raw HTTP) on the agent's "
                "hook thread will block the agent on every governance event."
            )
        self._track_event = track_event
        self._meta = runtime_metadata
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def accepts(self, event: AuditEvent) -> bool:
        """Filter the audit stream down to denials + non-empty hook summaries.

        Drops:

        - **Any event whose mode is** :attr:`EnforcementMode.DISABLED`
          — governance is off; emit nothing. The evaluator short-circuits
          before ``_emit_audit`` in this case, so in practice the sink
          shouldn't see these, but the guard here is belt-and-suspenders
          against any future emitter that bypasses the short-circuit.
        - Rules that didn't match (``matched=False``) — rolled into the
          hook summary's ``passed_count``.
        - Rules that matched but whose configured ``action`` is
          ``allow`` — a positive informational match isn't a denial;
          rolled into the hook summary too.
        - **Empty hook summaries** — ``total_rules=0`` AND
          ``skipped_count=0`` means nothing was evaluated and nothing
          was deliberately skipped. Zero operator value; suppressed.
          (A summary with ``skipped_count>0`` still fires because the
          ``skipped_policy_names`` payload is operator-relevant — it
          shows which policies a tenant turned off.)
        - Other event types (``hook_start``, ``session_*``, generic
          violations) — out of scope for this sink's telemetry surface.

        The result is bounded volume: at most one ``rule.denied`` event
        per matched-and-restrictive rule + at most one
        ``hook.summary`` per hook end (and only when it carries data).
        """
        if _mode_str(event.data.get("enforcement_mode")) == "DISABLED":
            return False

        if event.event_type == EventType.RULE_EVALUATION:
            if not event.data.get("matched"):
                return False
            action = str(event.data.get("action") or "allow").lower()
            return action != "allow"
        if event.event_type == EventType.HOOK_END:
            data = event.data
            total = int(data.get("total_rules") or 0)
            skipped = int(data.get("skipped_count") or 0)
            return not (total == 0 and skipped == 0)
        return False

    def emit(self, event: AuditEvent) -> None:
        """Render the event and dispatch via the injected callable.

        Re-checks ``matched`` for rule evaluations as a defense-in-depth
        guard. :meth:`accepts` already drops passed rules at the
        :class:`AuditManager` dispatch boundary, but a direct
        ``sink.emit(event)`` call (tests, future alternate dispatch
        paths) must not route a passed rule into the
        ``governance.rule.denied`` telemetry stream.

        Errors from ``track_event`` propagate to the audit manager,
        which has its own circuit breaker (10 consecutive failures →
        sink tripped for the rest of the process). The sink itself
        doesn't catch — letting the manager track failure rate is the
        whole point of the sink-failure protocol.
        """
        # DISABLED governance = no telemetry, full stop. Same
        # defense-in-depth rationale as the matched/allow check below:
        # ``accepts`` drops these at the manager boundary, but a
        # direct ``sink.emit`` call must not leak a DISABLED-mode
        # event downstream either.
        if _mode_str(event.data.get("enforcement_mode")) == "DISABLED":
            return

        if event.event_type == EventType.RULE_EVALUATION:
            if not event.data.get("matched"):
                return
            action = str(event.data.get("action") or "allow").lower()
            if action == "allow":
                # Positive informational match — not a denial. Same
                # defense-in-depth rationale as the ``matched`` check
                # above: ``accepts`` drops these at the manager
                # boundary, but a direct ``sink.emit`` call must not
                # leak them into the ``rule.denied`` stream.
                return
            self._emit_rule_denied(event)
        elif event.event_type == EventType.HOOK_END:
            data = event.data
            total = int(data.get("total_rules") or 0)
            skipped = int(data.get("skipped_count") or 0)
            if total == 0 and skipped == 0:
                # Empty hook — same defense-in-depth as the disabled
                # mode and matched-allow filters.
                return
            self._emit_hook_summary(event)
        # Other event types are filtered out at accepts(); reaching
        # here for anything else would be a contract violation by the
        # manager, not the sink.

    def _common_payload(self, event: AuditEvent) -> dict[str, Any]:
        """Per-runtime constants + event identifiers stamped on every payload."""
        payload: dict[str, Any] = dict(self._meta.as_payload())
        payload["agent_name"] = event.agent_name
        payload["hook"] = event.hook
        payload["timestamp"] = event.timestamp.isoformat()
        return payload

    def _emit_rule_denied(self, event: AuditEvent) -> None:
        """Emit one ``governance.rule.denied`` per matched rule."""
        data = event.data
        mode = _mode_str(data.get("enforcement_mode"))
        configured_action = str(data.get("action") or "allow")
        evaluator_result = _evaluator_result(configured_action)
        action_applied = _action_applied(evaluator_result, configured_action, mode)

        payload = self._common_payload(event)
        payload.update(
            {
                "pack": str(data.get("pack_name") or ""),
                "clause": str(data.get("policy_id") or ""),
                "rule_name": str(data.get("rule_name") or ""),
                "mode": mode,
                "evaluator_result": evaluator_result,
                "action_applied": action_applied,
                "duration_ms": float(data.get("duration_ms") or 0.0),
                "mapped_to_uipath": bool(data.get("mapped_to_uipath", False)),
                "detail": str(data.get("detail") or ""),
            }
        )
        self._track_event(
            event_name=EVENT_RULE_DENIED,
            data=payload,
            operation_id=_resolve_operation_id(),
        )

    def _emit_hook_summary(self, event: AuditEvent) -> None:
        """Emit one ``governance.hook.summary`` per hook end."""
        data = event.data
        mode = _mode_str(data.get("enforcement_mode"))

        payload = self._common_payload(event)
        payload.update(
            {
                "mode": mode,
                "passed_count": int(data.get("passed_count") or 0),
                "skipped_count": int(data.get("skipped_count") or 0),
                "skipped_policy_names": list(data.get("skipped_policy_names") or []),
                "denied_count": int(data.get("denied_count") or 0),
                "guardrail_dispatched_count": int(
                    data.get("guardrail_dispatched_count") or 0
                ),
                "duration_ms": float(data.get("duration_ms") or 0.0),
                "final_action": str(data.get("final_action") or "allow").upper(),
            }
        )
        self._track_event(
            event_name=EVENT_HOOK_SUMMARY,
            data=payload,
            operation_id=_resolve_operation_id(),
        )
