"""Console audit sink for human-readable governance output.

Writes audit events via Python logging (not stderr print), useful for
debugging and development.
"""
from __future__ import annotations

import json
import logging

from .base import AuditEvent, AuditSink, EventType

_logger = logging.getLogger("uipath.governance.audit.console")


class ConsoleAuditSink(AuditSink):
    """Audit sink that writes governance events via logging.

    Args:
        verbose: If True, show all events. If False, only show matches.
    """

    def __init__(self, verbose: bool = False) -> None:
        """Configure the sink's verbosity (verbose shows every event)."""
        self._verbose = verbose

    @property
    def name(self) -> str:
        """Constant sink identifier."""
        return "console"

    def accepts(self, event: AuditEvent) -> bool:
        """Filter to matched rules and lifecycle events unless verbose."""
        if self._verbose:
            return True
        if event.event_type == EventType.RULE_EVALUATION:
            return event.data.get("matched", False)
        return event.event_type in (
            EventType.SESSION_START,
            EventType.SESSION_END,
            EventType.HOOK_END,
            EventType.POLICY_VIOLATION,
        )

    def emit(self, event: AuditEvent) -> None:
        """Write the event to the log using the appropriate formatter."""
        if event.event_type == EventType.RULE_EVALUATION:
            self._emit_rule_evaluation(event)
        elif event.event_type == EventType.HOOK_END:
            self._emit_hook_summary(event)
        elif event.event_type == EventType.SESSION_START:
            self._emit_session_start(event)
        elif event.event_type == EventType.SESSION_END:
            self._emit_session_end(event)
        else:
            self._emit_generic(event)

    def _emit_rule_evaluation(self, event: AuditEvent) -> None:
        data = event.data
        matched = data.get("matched", False)
        status = "MATCHED" if matched else "PASS"
        policy_id = data.get("policy_id", "?")
        rule_name = data.get("rule_name", "?")
        action = data.get("action", "?").upper()
        detail = data.get("detail", "")

        if matched:
            _logger.warning(
                "[GOVERNANCE] [%s] %s | %s | action=%s | %s",
                status, policy_id, rule_name, action, detail,
            )
        elif self._verbose:
            _logger.info("[GOVERNANCE] [%s] %s | %s", status, policy_id, rule_name)

    def _emit_hook_summary(self, event: AuditEvent) -> None:
        data = event.data
        hook = event.hook
        total = data.get("total_rules", 0)
        matched = data.get("matched_rules", 0)
        action = data.get("final_action", "allow").upper()
        mode = data.get("enforcement_mode")
        mode_str = mode.value if hasattr(mode, "value") else str(mode or "audit")

        if mode_str == "audit" and action == "DENY":
            action = "AUDIT (would deny)"

        level = logging.WARNING if matched else logging.INFO
        _logger.log(
            level,
            "[GOVERNANCE] HOOK: %s | rules=%d | matched=%d | action=%s",
            hook, total, matched, action,
        )

    def _emit_session_start(self, event: AuditEvent) -> None:
        data = event.data
        packs = data.get("packs", [])
        mode = data.get("enforcement_mode")
        mode_str = mode.value if hasattr(mode, "value") else str(mode or "audit")
        _logger.info(
            "[GOVERNANCE] Session started | agent=%s | packs=%s | mode=%s",
            event.agent_name, ",".join(packs), mode_str,
        )

    def _emit_session_end(self, event: AuditEvent) -> None:
        data = event.data
        total = data.get("total_evaluations", 0)
        matched = data.get("rules_matched", 0)
        denied = data.get("rules_denied", 0)
        _logger.info(
            "[GOVERNANCE] Session ended | evaluations=%d | matched=%d | denied=%d",
            total, matched, denied,
        )

    def _emit_generic(self, event: AuditEvent) -> None:
        _logger.info(
            "[GOVERNANCE] %s | %s | %s",
            event.event_type, event.agent_name, json.dumps(event.data, default=str),
        )
