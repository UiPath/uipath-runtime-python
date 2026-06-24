"""OpenTelemetry traces audit sink for Orchestrator integration.

This sink creates OpenTelemetry spans for governance events. UiPath's
OTel exporter (``uipath.tracing._otel_exporters.LlmOpsHttpExporter`` via
``_SpanUtils.otel_span_to_uipath_span``) is what ships them to the
Orchestrator Traces UI and is also what reads ``UIPATH_TRACE_ID``,
``UIPATH_ORGANIZATION_ID``, ``UIPATH_TENANT_ID``, ``UIPATH_FOLDER_KEY``
and ``UIPATH_JOB_KEY`` from the process environment and stamps them onto
the outgoing ``UiPathSpan``. We intentionally do **not** duplicate that
env-reading here — the exporter is the single source of truth for the
job-execution context.
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

# Value for the ``type`` / ``span_type`` span attributes on every
# governance span. Matches ``SpanType.AGENT_RUN`` in uipath-agents-python
# — we use the string literal here (not a cross-package import) to keep
# uipath-runtime free of a uipath-agents dependency. If the agents-side
# registry adds new values, this constant is the single place to update.
SPAN_TYPE_AGENT_RUN = "agentRun"

# Identifies this auditor on every governance span. Lets a downstream
# consumer distinguish traces emitted by the Python in-runtime governance
# checker from those produced by the governance-server (or any future
# language-specific governance SDK). Set as the ``source`` span
# attribute on every governance trace span.
GOVERNANCE_SOURCE = "governance-checker-python"

# Shared attribute namespace for every key in the unified governance trace
# contract (§4 of the cross-product unification doc). Concatenated into
# each ``span.set_attribute`` call so the prefix appears in one place and
# a future rename (or alias) is a one-line change.
NS = "uipath_governance"

# Unified-contract enum values (UPPER_SNAKE per §3 of the spec).
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

    Mode travels with the event (set by :meth:`AuditManager.emit_rule_evaluation`
    / :meth:`emit_hook_summary` from the loader's per-instance mode) so
    the sink doesn't read a process-global that wouldn't be authoritative
    in a parallel-runtime setup.

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
    """Audit sink that creates OpenTelemetry spans.

    Spans appear in UiPath Orchestrator Traces UI, providing structured
    data for each governance evaluation.
    """

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
                # OpenTelemetry is supplied transitively by uipath-core; an
                # ImportError here means the host install is broken or
                # governance is running outside the UiPath SDK environment.
                logger.warning(
                    "OpenTelemetry not available - governance traces disabled. "
                    "OTel is normally provided by uipath-core; reinstall the SDK."
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

            # Use the current OTel context if one is active; otherwise start a
            # root span. A previous version fabricated a random parent
            # span_id when only a trace_id was known, which produced orphan
            # parents the backend could never resolve. The governance span
            # now correctly appears as a child of whichever span is current
            # (e.g. the runtime's root span) or as a fresh root.
            #
            # We don't touch org/tenant/folder/job/trace ids here — the
            # uipath OTel exporter resolves those at export time from the
            # process env (see module docstring).
            ctx = context.get_current()

            with tracer.start_as_current_span(span_name, context=ctx) as span:
                # Required for Orchestrator Traces
                span.set_attribute("type", SPAN_TYPE_AGENT_RUN)
                span.set_attribute("span_type", SPAN_TYPE_AGENT_RUN)
                span.set_attribute("uipath.custom_instrumentation", True)
                if event.trace_id:
                    span.set_attribute("uipath.trace_id", event.trace_id)

                # Identifies which agent emitted this audit trace. Lets
                # downstream consumers (Orchestrator Traces UI, audit
                # dashboards) filter governance spans by producer when
                # multiple SDKs / governance backends co-exist.
                span.set_attribute(f"{NS}.source", GOVERNANCE_SOURCE)
                # Hook summary attributes. Mode comes from the event — the
                # evaluator stamps it from the per-loader instance, so the
                # sink is correct for parallel runtimes running different
                # modes.
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

                # Hook spans are summary containers — they're left at
                # Status.UNSET regardless of final_action. Severity is
                # carried by the per-rule spans (see _emit_rule_span);
                # marking the hook span as ERROR would falsely paint
                # the entire lifecycle phase as failed when only a
                # specific rule fired underneath.

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

            # See note in _emit_hook_span: rely on the current OTel context
            # rather than fabricating a remote-parent span_id; and let the
            # uipath OTel exporter populate the job-execution context.
            ctx = context.get_current()

            with tracer.start_as_current_span(span_name, context=ctx) as span:
                # Required for Orchestrator Traces
                span.set_attribute("type", SPAN_TYPE_AGENT_RUN)
                span.set_attribute("span_type", SPAN_TYPE_AGENT_RUN)
                span.set_attribute("uipath.custom_instrumentation", True)
                if event.trace_id:
                    span.set_attribute("uipath.trace_id", event.trace_id)

                # Identifies which agent emitted this audit trace. Lets
                # downstream consumers (Orchestrator Traces UI, audit
                # dashboards) filter governance spans by producer when
                # multiple SDKs / governance backends co-exist.
                span.set_attribute(f"{NS}.source", GOVERNANCE_SOURCE)

                # Derive the spec-vocabulary verdict pair from the raw
                # (matched, configured action, mode) tuple. Mode comes
                # from the event (per-loader instance) so parallel
                # runtimes running different modes don't cross-contaminate.
                # Single source of truth for the emitted attributes below
                # AND the verbosityLevel/Status decision further down.
                mode = _resolve_mode(event)
                configured_action = data.get("action", "allow")
                matched = bool(data.get("matched", False))
                evaluator_result, action_applied = _derive_results(
                    matched=matched,
                    configured_action=configured_action,
                    mode=mode,
                )

                # Governance attributes
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
                #
                # - ``DENY`` — runtime actually blocked the agent →
                #   verbosityLevel=4 (Error) + Status.ERROR. The agent
                #   span genuinely failed.
                # - ``AUDIT`` / ``HITL`` — advisory only; runtime did NOT
                #   block → verbosityLevel=3 (Warning), Status stays
                #   UNSET. The agent's span shouldn't be marked failed
                #   just because an advisory rule fired.
                # - ``ALLOW`` / ``NONE`` — no verbosityLevel attribute
                #   (platform default = 2, Information).
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
