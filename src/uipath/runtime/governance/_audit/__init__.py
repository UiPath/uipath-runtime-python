"""Audit sink framework for governance events.

Internal module. Provides a pluggable audit system that emits
governance events to one or more sinks. The built-in
:class:`TracesAuditSink` emits OpenTelemetry spans and is always
registered by every :class:`AuditManager` — it carries the governance
audit trail and cannot be disabled by application code.

Callers import from the submodules directly (``_audit.base``,
``_audit.traces``, ``_audit.factory``). This package exposes no
aggregated symbols.
"""
