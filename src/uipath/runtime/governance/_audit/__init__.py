"""Audit sink framework for governance events.

Internal module. Provides a pluggable audit system that emits governance
events to one or more sinks. The only built-in sink is ``TracesAuditSink``,
which creates OpenTelemetry spans that uipath-core's exporter ships to the
Orchestrator Traces UI. This sink is always registered by every
:class:`AuditManager` and cannot be disabled by application code — it
carries the governance audit trail.

Callers import from the submodules directly (``_audit.base``, ``_audit.traces``,
``_audit.factory``). This package exposes no aggregated symbols.
"""
