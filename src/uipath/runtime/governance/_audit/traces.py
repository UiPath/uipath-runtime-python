"""OpenTelemetry traces audit sink for governance events.

Emits an OpenTelemetry span per rule evaluation and per hook summary.
This sink emits spans only — it does not resolve or stamp
job-execution metadata (organization, tenant, folder, job, trace id)
onto them. That resolution is owned by the platform-side OTel
exporter that ships spans downstream, so the runtime governance
contract stays scoped to span emission.
"""

from __future__ import annotations

import importlib.metadata
import logging
from typing import Any

from uipath.core.governance import EnforcementMode

from .base import AuditEvent, AuditSink, EventType

logger = logging.getLogger(__name__)


def _package_version() -> str:
    """Return the installed ``uipath-runtime`` version (``unknown`` if absent)."""
    try:
        return importlib.metadata.version("uipath-runtime")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


# Stamped on every governance span as ``uipath_governance.version`` so
# consumers can correlate the trace payload shape with the runtime
# release that produced it. Resolved once at import time — the installed
# package version doesn't change for the life of the process.
SCHEMA_VERSION = _package_version()

# Value of the ``type`` / ``span_type`` span attributes on every
# governance span. Local to the runtime trace contract — kept as a
# string literal (not a cross-package import) so the runtime stays
# self-contained.
SPAN_TYPE_AGENT_RUN = "agentRun"

# Set as the ``source`` attribute on every governance span. Lets
# consumers identify which producer emitted a given span when more
# than one governance producer feeds the same trace backend.
GOVERNANCE_SOURCE = "governance-checker-python"

# Shared attribute namespace for every governance span attribute.
# Concatenated into each ``span.set_attribute`` call so the prefix
# appears in one place and a future rename is a one-line change.
NS = "uipath_governance"

# Governance verdict / action vocabulary (UPPER_SNAKE).
EVALUATOR_ALLOW = "ALLOW"
EVALUATOR_DENY = "DENY"
EVALUATOR_HITL = "HITL"

ACTION_ALLOW = "ALLOW"
ACTION_DENY = "DENY"
ACTION_HITL = "HITL"
ACTION_AUDIT = "AUDIT"
ACTION_NONE = "NONE"

def _resolve_mode(event: AuditEvent) -> EnforcementMode:
    """Read the enforcement mode the evaluator stamped on the event.

    Mode travels with the event (set by the emitter when it calls
    :meth:`AuditManager.emit_rule_evaluation` /
    :meth:`emit_hook_summary` and passes its own per-instance mode) so
    the sink doesn't read a process-global that wouldn't be
    authoritative in a parallel-runtime setup.

    Falls back to ``AUDIT`` only when the field is missing — that's a
    contract violation by the emitter (every governance event must carry
    the mode), but defaulting to the safe option avoids a sink crash.
    """
    mode = event.data.get("enforcement_mode")
    if isinstance(mode, EnforcementMode):
        return mode
    if isinstance(mode, str):
        try:
            return EnforcementMode(mode.lower())
        except ValueError:
            pass
    return EnforcementMode.AUDIT


def _derive_results(
    matched: bool, configured_action: str, mode: EnforcementMode
) -> tuple[str, str]:
    """Return ``(evaluator_result, action_applied)`` in spec vocabulary.

    ``evaluator_result`` is mode-independent — what the rule decided. The
    rule's configured ``audit`` action collapses into a DENY decision
    here; whether that DENY is actually applied is reflected in
    ``action_applied``.

    ``action_applied`` is mode-driven. Currently only AUDIT mode is wired
    in the runtime, so every non-allow result lands on ``AUDIT``; the
    ENFORCE branch is kept so the contract is already correct when
    ENFORCE arrives in a later phase.

    The configured ``audit`` rule-level action acts as a per-rule audit
    override: even when global mode is ENFORCE, such a rule only ever
    produces ``action_applied = AUDIT``. This preserves today's "audit
    never blocks" behavior.
    """
    action = configured_action.lower()

    if not matched or action == "allow":
        return EVALUATOR_ALLOW, ACTION_NONE

    if action == "escalate":
        evaluator = EVALUATOR_HITL
    else:
        evaluator = EVALUATOR_DENY

    # Per-rule audit override: emit AUDIT regardless of global mode.
    if action == "audit":
        return evaluator, ACTION_AUDIT

    if mode == EnforcementMode.ENFORCE:
        return evaluator, ACTION_DENY if evaluator == EVALUATOR_DENY else ACTION_HITL
    return evaluator, ACTION_AUDIT

class TracesAuditSink(AuditSink):
    """Audit sink that emits an OpenTelemetry span per governance event."""

    def __init__(self) -> None:
        """Initialize the sink with a deferred tracer and zero span count."""
        self._tracer: Any = None  # Can be None, Tracer, or False
        self._spans_created = 0

    @property
    def name(self) -> str:
        """Constant sink identifier."""
        return "traces"

    def _get_tracer(self) -> Any:
        """Get or create the OpenTelemetry tracer."""
        if self._tracer is None:
            try:
                from opentelemetry import trace

                self._tracer = trace.get_tracer("uipath.governance")
                logger.info("OpenTelemetry tracer initialized for governance traces")
            except ImportError:
                logger.warning(
                    "OpenTelemetry not available — governance traces disabled."
                )
                self._tracer = False
        return self._tracer if self._tracer else None

    def emit(self, event: AuditEvent) -> None:
        """Create a span for RULE_EVALUATION or HOOK_END events; drop others."""
        if event.event_type == EventType.RULE_EVALUATION:
            self._emit_rule_span(event)
        elif event.event_type == EventType.HOOK_END:
            self._emit_hook_span(event)

    def _emit_hook_span(self, event: AuditEvent) -> None:
        """Create a span for a hook summary (always emitted for each governance check)."""
        tracer = self._get_tracer()
        if tracer is None:
            return

        try:
            from opentelemetry import context

            data = event.data
            hook = event.hook or "unknown"
            span_name = f"governance.{hook.lower()}"

            # Use the current OTel context. The audit manager runs the
            # sink inside the caller's captured ``contextvars`` context
            # (see :meth:`AuditManager.emit`), so the agent's live span
            # is visible here even on the audit worker thread — the
            # governance span attaches as a child instead of orphan root.
            ctx = context.get_current()

            with tracer.start_as_current_span(span_name, context=ctx) as span:
                span.set_attribute("type", SPAN_TYPE_AGENT_RUN)
                span.set_attribute("span_type", SPAN_TYPE_AGENT_RUN)
                span.set_attribute("uipath.custom_instrumentation", True)
                span.set_attribute(f"{NS}.source", GOVERNANCE_SOURCE)

                # Mode travels on the event so parallel runtimes running
                # different per-instance modes don't cross-contaminate.
                mode = _resolve_mode(event)
                final_action = data.get("final_action", "allow")
                _, action_applied = _derive_results(
                    matched=final_action.lower() != "allow",
                    configured_action=final_action,
                    mode=mode,
                )
                span.set_attribute(f"{NS}.hook", hook)
                span.set_attribute(f"{NS}.action_applied", action_applied)
                span.set_attribute(f"{NS}.mode", mode.value.upper())

                # Hook spans are summary containers — severity lives on
                # the per-rule spans. Marking the hook ERROR would paint
                # the whole lifecycle phase as failed when only one rule
                # fired beneath it.

                self._spans_created += 1

        except Exception as e:
            logger.warning("Failed to create governance hook span: %s", e)

    def _emit_rule_span(self, event: AuditEvent) -> None:
        """Create a span for a rule evaluation."""
        tracer = self._get_tracer()
        if tracer is None:
            return

        try:
            from opentelemetry import context

            data = event.data
            policy_id = data.get("policy_id", "unknown")
            span_name = f"{NS}.rule.{policy_id}"

            # See _emit_hook_span: the contextvars-captured caller
            # context means the current OTel context is the agent's
            # live span, so this rule span attaches as its child.
            ctx = context.get_current()

            with tracer.start_as_current_span(span_name, context=ctx) as span:
                span.set_attribute("type", SPAN_TYPE_AGENT_RUN)
                span.set_attribute("span_type", SPAN_TYPE_AGENT_RUN)
                span.set_attribute("uipath.custom_instrumentation", True)
                span.set_attribute(f"{NS}.source", GOVERNANCE_SOURCE)

                # Single source of truth for the emitted attributes
                # below AND the verbosityLevel / Status decision further
                # down. Mode comes from the event (per-instance) so
                # parallel runtimes don't cross-contaminate.
                mode = _resolve_mode(event)
                configured_action = data.get("action", "allow")
                matched = bool(data.get("matched", False))
                evaluator_result, action_applied = _derive_results(
                    matched=matched,
                    configured_action=configured_action,
                    mode=mode,
                )

                span.set_attribute(f"{NS}.policy_id", policy_id)
                span.set_attribute(f"{NS}.rule_name", data.get("rule_name", ""))
                span.set_attribute(f"{NS}.pack_name", data.get("pack_name", ""))
                span.set_attribute(f"{NS}.hook", event.hook)
                span.set_attribute(f"{NS}.evaluator_result", evaluator_result)
                span.set_attribute(f"{NS}.action_applied", action_applied)
                span.set_attribute(f"{NS}.mode", mode.value.upper())
                span.set_attribute(f"{NS}.version", SCHEMA_VERSION)

                detail = data.get("detail", "")
                if detail:
                    span.set_attribute(f"{NS}.evidence", detail[:500])

                # Severity is driven off the derived ``action_applied``:
                # - DENY — runtime blocked → verbosityLevel=4 +
                #   Status.ERROR (agent span genuinely failed).
                # - AUDIT / HITL — advisory, runtime did not block →
                #   verbosityLevel=3, Status stays UNSET. Marking the
                #   agent span failed for an advisory rule would mislead.
                # - ALLOW / NONE — no verbosityLevel attribute set.
                if action_applied == ACTION_DENY:
                    span.set_attribute("verbosityLevel", 4)
                    try:
                        from opentelemetry.trace import Status, StatusCode

                        span.set_status(
                            Status(
                                StatusCode.ERROR,
                                f"Policy violation: "
                                f"{data.get('rule_name', policy_id)} "
                                f"(action={configured_action.lower()})",
                            )
                        )
                    except ImportError:
                        pass
                elif action_applied in (ACTION_AUDIT, ACTION_HITL):
                    span.set_attribute("verbosityLevel", 3)

                self._spans_created += 1

        except Exception as e:
            logger.warning("Failed to create governance span: %s", e)

    @property
    def spans_created(self) -> int:
        """Number of spans created."""
        return self._spans_created
