"""OpenTelemetry traces audit sink for Orchestrator integration.

This sink creates OpenTelemetry spans for governance events, which
appear in the UiPath Orchestrator Traces UI for observability.
"""

from __future__ import annotations

import logging
from typing import Any

from .base import AuditEvent, AuditSink, EventType

logger = logging.getLogger(__name__)

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

    def _get_uipath_trace_id(self) -> str | None:
        """Get trace ID from UiPath config."""
        try:
            from uipath.platform.common import UiPathConfig

            return UiPathConfig.trace_id
        except (ImportError, AttributeError):
            return None

    def _get_uipath_context(self) -> dict[str, str]:
        """Get UiPath context attributes."""
        context = {}
        try:
            from uipath.platform.common import UiPathConfig

            if UiPathConfig.organization_id:
                context["uipath.organization_id"] = UiPathConfig.organization_id
            if UiPathConfig.tenant_id:
                context["uipath.tenant_id"] = UiPathConfig.tenant_id
            if UiPathConfig.folder_key:
                context["uipath.folder_key"] = UiPathConfig.folder_key
            if UiPathConfig.job_key:
                context["uipath.job_key"] = UiPathConfig.job_key
        except (ImportError, AttributeError):
            pass
        return context

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
            ctx = context.get_current()
            uipath_trace_id = event.trace_id or self._get_uipath_trace_id()

            with tracer.start_as_current_span(span_name, context=ctx) as span:
                # Required for Orchestrator Traces
                span.set_attribute("type", SPAN_TYPE_AGENT_RUN)
                span.set_attribute("span_type", SPAN_TYPE_AGENT_RUN)
                # Identifies which agent emitted this audit trace. Lets
                # downstream consumers (Orchestrator Traces UI, audit
                # dashboards) filter governance spans by producer when
                # multiple SDKs / governance backends co-exist.
                span.set_attribute("source", GOVERNANCE_SOURCE)
                span.set_attribute("uipath.custom_instrumentation", True)
                if uipath_trace_id:
                    span.set_attribute("uipath.trace_id", uipath_trace_id)

                # UiPath context
                for key, value in self._get_uipath_context().items():
                    span.set_attribute(key, value)

                # Hook summary attributes
                span.set_attribute("governance.hook", hook)
                span.set_attribute("governance.total_rules", data.get("total_rules", 0))
                span.set_attribute(
                    "governance.matched_rules", data.get("matched_rules", 0)
                )
                span.set_attribute(
                    "governance.final_action", data.get("final_action", "allow")
                )
                span.set_attribute(
                    "governance.enforcement_mode", data.get("enforcement_mode", "audit")
                )
                span.set_attribute("governance.agent_name", event.agent_name)

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
            rule_id = data.get("rule_id", "unknown")
            span_name = f"governance.rule.{rule_id}"

            # See note in _emit_hook_span: rely on the current OTel context
            # rather than fabricating a remote-parent span_id.
            ctx = context.get_current()
            uipath_trace_id = event.trace_id or self._get_uipath_trace_id()

            with tracer.start_as_current_span(span_name, context=ctx) as span:
                # Required for Orchestrator Traces
                span.set_attribute("type", SPAN_TYPE_AGENT_RUN)
                span.set_attribute("span_type", SPAN_TYPE_AGENT_RUN)
                # Identifies which agent emitted this audit trace. Lets
                # downstream consumers (Orchestrator Traces UI, audit
                # dashboards) filter governance spans by producer when
                # multiple SDKs / governance backends co-exist.
                span.set_attribute("source", GOVERNANCE_SOURCE)
                span.set_attribute("uipath.custom_instrumentation", True)
                if uipath_trace_id:
                    span.set_attribute("uipath.trace_id", uipath_trace_id)

                # UiPath context
                for key, value in self._get_uipath_context().items():
                    span.set_attribute(key, value)

                # Governance attributes
                span.set_attribute("governance.rule_id", rule_id)
                span.set_attribute("governance.rule_name", data.get("rule_name", ""))
                span.set_attribute("governance.pack_name", data.get("pack_name", ""))
                span.set_attribute("governance.hook", event.hook)
                span.set_attribute("governance.matched", data.get("matched", False))
                span.set_attribute("governance.action", data.get("action", "allow"))
                span.set_attribute("governance.status", data.get("status", "PASS"))
                span.set_attribute("governance.agent_name", event.agent_name)

                detail = data.get("detail", "")
                if detail:
                    span.set_attribute("governance.detail", detail[:500])

                # Severity for matched non-allow rules is carried by the
                # platform-standard ``verbosityLevel`` span field (UiPath
                # Orchestrator log levels: 3=Warning, 4=Error). Default
                # platform verbosity is 2 (Information), so we only set
                # this attribute when there's a violation worth flagging.
                #
                # - Audit mode (and any audit-action rule even in
                #   enforce mode): runtime did NOT block the agent →
                #   verbosityLevel=3 (Warning), Status stays UNSET. The
                #   agent's span shouldn't be marked failed just because
                #   an advisory rule fired.
                # - Enforce mode + deny / escalate: runtime actually
                #   blocked → verbosityLevel=4 (Error) + Status.ERROR.
                #   The agent span genuinely failed.
                action_str = data.get("action", "allow").lower()
                if data.get("matched") and action_str != "allow":
                    from uipath.runtime.governance.config import (
                        EnforcementMode,
                        get_enforcement_mode,
                    )

                    mode = get_enforcement_mode()
                    will_block = (
                        mode == EnforcementMode.ENFORCE
                        and action_str in {"deny", "escalate"}
                    )
                    span.set_attribute("verbosityLevel", 4 if will_block else 3)
                    if will_block:
                        try:
                            from opentelemetry.trace import StatusCode

                            span.set_status(
                                StatusCode.ERROR,
                                f"Policy violation: "
                                f"{data.get('rule_name', rule_id)} "
                                f"(action={action_str})",
                            )
                        except ImportError:
                            pass

                self._spans_created += 1

        except Exception as e:
            logger.warning("Failed to create governance span: %s", e)

    @property
    def spans_created(self) -> int:
        """Number of spans created."""
        return self._spans_created
