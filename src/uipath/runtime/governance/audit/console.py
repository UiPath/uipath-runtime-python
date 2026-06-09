"""Console audit sink for human-readable output.

This sink writes audit events to stderr in a human-readable format,
useful for debugging and development.
"""

from __future__ import annotations

import json
import sys

from .base import AuditEvent, AuditSink, EventType


class ConsoleAuditSink(AuditSink):
    """Audit sink that writes to console (stderr).

    Useful for debugging and development. Output is human-readable.

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
        # Only show matched rules and important events
        if event.event_type == EventType.RULE_EVALUATION:
            return event.data.get("matched", False)
        return event.event_type in (
            EventType.SESSION_START,
            EventType.SESSION_END,
            EventType.HOOK_END,
            EventType.POLICY_VIOLATION,
        )

    def emit(self, event: AuditEvent) -> None:
        """Write the event to stderr using the appropriate formatter."""
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
        rule_id = data.get("rule_id", "?")
        rule_name = data.get("rule_name", "?")
        action = data.get("action", "?").upper()
        detail = data.get("detail", "")

        if matched:
            print(
                f"[GOVERNANCE] [{status}] {rule_id} | {rule_name} | "
                f"action={action} | {detail}",
                file=sys.stderr,
                flush=True,
            )
        elif self._verbose:
            print(
                f"[GOVERNANCE] [{status}] {rule_id} | {rule_name}",
                file=sys.stderr,
                flush=True,
            )

    def _emit_hook_summary(self, event: AuditEvent) -> None:
        data = event.data
        hook = event.hook
        total = data.get("total_rules", 0)
        matched = data.get("matched_rules", 0)
        action = data.get("final_action", "allow").upper()
        mode = data.get("enforcement_mode", "audit")

        if mode == "audit" and action == "DENY":
            action = "AUDIT (would deny)"

        print(
            f"[GOVERNANCE] HOOK: {hook} | rules={total} | matched={matched} | "
            f"action={action}",
            file=sys.stderr,
            flush=True,
        )

    def _emit_session_start(self, event: AuditEvent) -> None:
        data = event.data
        packs = data.get("packs", [])
        mode = data.get("enforcement_mode", "audit")
        print(
            f"[GOVERNANCE] Session started | agent={event.agent_name} | "
            f"packs={','.join(packs)} | mode={mode}",
            file=sys.stderr,
            flush=True,
        )

    def _emit_session_end(self, event: AuditEvent) -> None:
        data = event.data
        total = data.get("total_evaluations", 0)
        matched = data.get("rules_matched", 0)
        denied = data.get("rules_denied", 0)
        print(
            f"[GOVERNANCE] Session ended | evaluations={total} | "
            f"matched={matched} | denied={denied}",
            file=sys.stderr,
            flush=True,
        )

    def _emit_generic(self, event: AuditEvent) -> None:
        print(
            f"[GOVERNANCE] {event.event_type} | {event.agent_name} | "
            f"{json.dumps(event.data)}",
            file=sys.stderr,
            flush=True,
        )
