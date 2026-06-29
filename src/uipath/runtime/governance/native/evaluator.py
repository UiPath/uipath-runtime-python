"""Governance rule evaluator.

Instance-scoped — every :class:`GovernanceRuntime` constructs its own
evaluator with explicit dependencies (audit manager, compensator,
enforcement mode). The evaluator does not reach across the runtime
layer through process-globals; the wiring layer composes the runtime
graph and the evaluator consumes what it's given.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

from uipath.core.governance import EnforcementMode
from uipath.core.governance.exceptions import GovernanceBlockException
from uipath.core.governance.models import (
    Action,
    AuditRecord,
    LifecycleHook,
    RuleEvaluation,
)

from uipath.runtime.governance._audit.base import AuditManager
from uipath.runtime.governance.native.guardrail_compensation import (
    GuardrailCompensator,
    disabled_guardrails,
)
from uipath.runtime.governance.native.models import (
    Check,
    CheckContext,
    Condition,
    PolicyIndex,
    Rule,
)

logger = logging.getLogger(__name__)


def _compensation_data_for_hook(context: CheckContext) -> dict[str, Any]:
    """Build the ``data`` payload for the compensating-governance call.

    Forwards whichever :class:`CheckContext` field is populated for the
    active hook so the compensating call evaluates the same content
    the evaluator was looking at. Fields not relevant to the hook are
    omitted to keep the payload tight.
    """
    if context.hook in (LifecycleHook.BEFORE_AGENT,):
        return {"content": context.agent_input}
    if context.hook in (LifecycleHook.AFTER_AGENT,):
        return {"content": context.agent_output}
    if context.hook in (LifecycleHook.BEFORE_MODEL,):
        payload: dict[str, Any] = {"content": context.model_input}
        if context.messages:
            payload["messages"] = context.messages
        return payload
    if context.hook in (LifecycleHook.AFTER_MODEL,):
        return {"content": context.model_output}
    if context.hook in (LifecycleHook.TOOL_CALL,):
        return {"tool_name": context.tool_name, "tool_args": context.tool_args}
    if context.hook in (LifecycleHook.AFTER_TOOL,):
        return {"tool_name": context.tool_name, "tool_result": context.tool_result}
    # Memory-write and unknown hooks: pass an empty content so the
    # server still receives a structurally-valid payload.
    return {"content": ""}


@lru_cache(maxsize=256)
def _compile_regex(pattern: str) -> re.Pattern[str] | None:
    """Compile and cache a regex pattern.

    Args:
        pattern: The regex pattern string

    Returns:
        Compiled pattern or None if invalid
    """
    try:
        return re.compile(pattern)
    except re.error as e:
        logger.warning("Invalid regex pattern '%s': %s", pattern, e)
        return None


# --- vaderSentiment: lazy-imported singleton ---
# Hard dependency, but lazy-loaded to keep import-time cost off the
# critical path. The except branch is defence against a corrupted
# install (file present in METADATA but module unimportable) — the
# operator no-ops rather than crashing the agent.
_VADER_UNINITIALIZED = object()
_vader_analyzer: Any = _VADER_UNINITIALIZED


def _get_vader_analyzer() -> Any:
    """Return a cached SentimentIntensityAnalyzer, or None if unavailable."""
    global _vader_analyzer
    if _vader_analyzer is _VADER_UNINITIALIZED:
        try:
            from vaderSentiment.vaderSentiment import (  # type: ignore[import-untyped]
                SentimentIntensityAnalyzer,
            )

            _vader_analyzer = SentimentIntensityAnalyzer()
        except ImportError:
            logger.error(
                "vaderSentiment failed to import despite being a hard dependency; "
                "sentiment_concern checks will not fire."
            )
            _vader_analyzer = None
    return _vader_analyzer


# --- chardet: lazy-imported module for encoding integrity (A.7.4) ---
# Hard dependency, lazy-loaded for symmetry with the other library
# wrappers. The except branch covers corrupted installs only.
_CHARDET_UNINITIALIZED = object()
_chardet_module: Any = _CHARDET_UNINITIALIZED


def _get_chardet() -> Any:
    """Return the chardet module, or None if unavailable."""
    global _chardet_module
    if _chardet_module is _CHARDET_UNINITIALIZED:
        try:
            import chardet

            _chardet_module = chardet
        except ImportError:
            logger.error(
                "chardet failed to import despite being a hard dependency; "
                "encoding_concern confidence check will not fire (stdlib "
                "signals still apply)."
            )
            _chardet_module = None
    return _chardet_module


# --- Static patterns for encoding_concern (A.7.4) ---
# Latin-1-as-UTF-8 mojibake bigrams — the visible artefacts when
# UTF-8-encoded text is re-decoded as Latin-1 / Windows-1252.
_MOJIBAKE_BIGRAMS: tuple[str, ...] = (
    "Ã©",
    "Ã¨",
    "Ã¢",
    "Ã ",
    "Ã¹",
    "Ã®",
    "Ã´",
    "Ã§",  # accented vowels
    "Ã„",
    "Ã–",
    "Ãœ",
    "ÃŸ",  # German umlauts / eszett
    "â€™",
    "â€œ",
    "â€\x9d",
    "â€“",
    "â€”",
    "â€¢",  # smart quotes / dashes
    "Â£",
    "Â°",
    "Â§",
    "Â¶",
    "Â©",
    "Â®",  # NBSP-leading symbols
    "ï¿",
    "¿½",  # mojibake'd U+FFFD (0xEF 0xBF 0xBD as Latin-1)
    "ï»",
    "»¿",  # mojibake'd BOM (0xEF 0xBB 0xBF as Latin-1)
)

# Literal hex escape sequences ("\x80" as 4 source chars) indicate raw
# bytes leaked through a string layer rather than being decoded.
_HEX_ESCAPE_PATTERN = re.compile(r"\\x[0-9a-fA-F]{2}")


# --- Static patterns for incident_concern (A.8.4) ---
# Stdlib-only categorical taxonomy. Mirrors sentry-sdk's incident shape
# (categorical types over stack/status), but for string payloads from
# model output / tool result rather than exception objects.
_INCIDENT_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "safety_refusal": [
        re.compile(
            r"(?i)\b(i\s+(?:cannot|can'?t|am\s+unable\s+to|won'?t\s+be\s+able\s+to)"
            r"\s+(?:help|assist|provide|answer|do\s+that))\b"
        ),
        re.compile(r"(?i)\b(i'?m\s+sorry,?\s+but\s+i\s+(?:cannot|can'?t))\b"),
        re.compile(r"(?i)\b(against\s+my\s+(?:guidelines|policies|programming))\b"),
    ],
    "tool_failure": [
        re.compile(
            r"\b(5\d{2})\b\s*(?:internal\s+server\s+error|service\s+unavailable)"
        ),
        re.compile(r"(?i)\b(ERR_[A-Z_]+|connection\s+refused|ECONNREFUSED)\b"),
        re.compile(r"(?i)\b(timed?\s*out|timeout)\b"),
    ],
    "auth_failure": [
        re.compile(r"\b(401|403)\b\s*(?:unauthori[sz]ed|forbidden)"),
        re.compile(
            r"(?i)\b(authentication\s+failed|invalid\s+(?:token|credentials))\b"
        ),
    ],
    "quota_exceeded": [
        re.compile(r"\b(429)\b"),
        re.compile(
            r"(?i)\b(rate\s+limit\s+exceeded|quota\s+exceeded|too\s+many\s+requests)\b"
        ),
    ],
    "hallucination": [
        re.compile(r"(?i)\b(i\s+(?:made\s+(?:that|this)\s+up|am\s+just\s+guessing))\b"),
        re.compile(r"(?i)\b(i\s+don'?t\s+actually\s+know|i\s+fabricat(?:ed|ing))\b"),
    ],
}

# --- Static patterns for commitment_concern (A.10.4) ---
# Commitment-language signals. The verb pattern covers both first-person
# promise verbs ("we will refund") and formal-business commitment markers
# common in proposal / SOW outputs ("Cost: $X", "fixed scope",
# "Deliverables", "Timeline: N days", "I propose"). Verb, amount, and
# deadline signals combine via OR semantics — see
# :meth:`_check_commitment_concern`.
_COMMITMENT_VERB_PATTERN = re.compile(
    r"(?i)("
    # First-person promise / liability verbs
    r"\brefund\b|\breimburse\b|"
    r"\bwarranty\b|\bwarrant(?:y|ed|ies)\b|\bguarante[ed]+\b|"
    r"\bsla\b|"
    r"\bwaive[d]?\b|"
    r"\b(?:we|i)\s+(?:will|shall|promise|commit|guarantee)\b|"
    r"\b(?:we|i|i'?ll)\s+(?:deliver|provide|complete|finish|"
    r"handover|hand\s+over|ship)\b|"
    # Proposal / SOW commitment markers
    r"\bfixed\s+(?:price|cost|fee|scope|bid|rate)\b|"
    r"\bcost\s*:\s*\$?\d|"
    r"\bquote\s*:\s*\$?\d|"
    r"\bdeliverables?\b|"
    r"\btimeline\s*:\s*\d+\s*(?:second|minute|hour|day|week|month|year)s?\b|"
    r"\bI\s+propose\b"
    r")"
)
# Currency-anchored amount detection. Requires a currency marker adjacent
# to the number so URL fragments (e.g. ``/667851``) don't false-positive.
# Covers symbol-then-number ($780) and number-then-code (780 USD).
#
# Bare percentages (``75%``, ``99.9%``) are deliberately NOT matched
# here — they fire on benign status / progress text ("75% complete",
# "99.9% uptime") under OR semantics. Real percentage-bearing
# commitments ("we'll give you a 20% discount", "refund 100%") still
# fire via the verb pattern.
_COMMITMENT_AMOUNT_FALLBACK = re.compile(
    r"(?:\$|€|£|¥|₹|USD|EUR|GBP|JPY|INR)\s*\d[\d,]*(?:\.\d+)?"
    r"|\b\d[\d,]*(?:\.\d+)?\s*(?:USD|EUR|GBP|JPY|INR|"
    r"dollars?|euros?|pounds?|yen|rupees?)\b"
)
_COMMITMENT_DEADLINE_PATTERN = re.compile(
    r"(?i)\bwithin\s+\d+\s*(?:second|minute|hour|day|week|month|year)s?\b"
    r"|\bby\s+(?:tomorrow|next\s+\w+|\d+/\d+(?:/\d+)?)\b"
)


class GovernanceEvaluator:
    """Evaluates governance rules against check contexts.

    Supports two enforcement modes:

    - ``AUDIT``: log all violations but never block (DENY collapses to
      AUDIT in the final action).
    - ``ENFORCE``: actually block on DENY rules — raises
      :class:`GovernanceBlockException` and the agent stops.

    All dependencies (mode, audit manager, compensator) are injected
    via the constructor. The evaluator does not consult any
    process-global state — parallel runtimes (``uipath eval``) get
    their own evaluator with their own audit + compensation pipelines.
    """

    def __init__(
        self,
        policy_index: PolicyIndex,
        *,
        enforcement_mode: EnforcementMode = EnforcementMode.AUDIT,
        audit_manager: AuditManager | None = None,
        compensator: GuardrailCompensator | None = None,
    ) -> None:
        """Initialize with a compiled policy index and runtime-scoped deps.

        Args:
            policy_index: The compiled :class:`PolicyIndex` to evaluate.
                Typically read from :attr:`GovernanceRuntime.policy_index`
                — the host built it from the provider's
                :class:`PolicyResponse` via
                :func:`build_policy_index_from_yaml`.
            enforcement_mode: Mode the evaluator applies. Defaults to
                ``AUDIT`` — the safe default for callers that don't
                explicitly opt in to ENFORCE. The wiring layer should
                pass ``runtime.enforcement_mode`` here so the evaluator
                and the wrapping :class:`GovernanceRuntime` agree on a
                single source of truth.
            audit_manager: Per-runtime :class:`AuditManager`. When
                ``None`` the evaluator runs silently (no audit events
                emitted). Tests that don't care about emission can
                leave this out.
            compensator: Per-runtime :class:`GuardrailCompensator`
                used to dispatch ``/runtime/govern`` POSTs for
                guardrail-fallback rules. When ``None`` such dispatch
                is skipped — the evaluator still records the matched
                rules in the :class:`AuditRecord`.
        """
        self._policy_index = policy_index
        self._enforcement_mode = enforcement_mode
        self._audit_manager = audit_manager
        self._compensator = compensator

    @property
    def policy_index(self) -> PolicyIndex:
        """Return the compiled policy index this evaluator runs against."""
        return self._policy_index

    @property
    def mode(self) -> EnforcementMode:
        """The enforcement mode this evaluator applies."""
        return self._enforcement_mode

    def is_audit_mode(self) -> bool:
        """Check if running in audit-only mode."""
        return self._enforcement_mode == EnforcementMode.AUDIT

    def evaluate(self, context: CheckContext) -> AuditRecord:
        """Evaluate rules registered for ``context.hook`` against the context.

        Only rules whose ``hook`` field matches the current lifecycle hook
        are evaluated — a ``tool_call`` rule does not fire on
        ``before_model``, and vice versa. This avoids running checks
        against fields the context cannot provide and keeps the audit
        stream scoped to the active phase.

        The final action depends on the enforcement mode:
        - DISABLED mode: Short-circuit; no rules evaluated, no audit emitted.
        - AUDIT mode: Even DENY rules result in AUDIT action (log only, don't block)
        - ENFORCE mode: DENY rules result in DENY action AND a
          :class:`GovernanceBlockException` is raised.

        Audit events (per-rule + hook summary) are emitted via the
        :class:`AuditManager` injected at construction (skipped when
        none was supplied).

        Args:
            context: The check context with hook and content

        Returns:
            AuditRecord with all evaluations and final action.

        Raises:
            GovernanceBlockException: In ENFORCE mode when a DENY rule matches.
        """
        mode = self._enforcement_mode
        if mode == EnforcementMode.DISABLED:
            return AuditRecord(
                timestamp=datetime.now(timezone.utc),
                agent_name=context.agent_name,
                runtime_id=context.runtime_id,
                hook=context.hook,
                evaluations=[],
                final_action=Action.ALLOW,
                metadata={**context.metadata, "enforcement_mode": mode.value},
            )

        rules = self._policy_index.get_rules_for_hook(context.hook)

        evaluations: list[RuleEvaluation] = []
        raw_action = Action.ALLOW  # The action before mode adjustment
        deny_would_fire = False  # Track if DENY would have fired

        for rule in rules:
            if not rule.enabled:
                continue

            evaluation = self._evaluate_rule(rule, context)
            evaluations.append(evaluation)

            if evaluation.matched:
                # Take the most restrictive action. Use evaluation.action
                # (which already folds in per-check overrides), not
                # rule.action, so check-level overrides are honored here too.
                eval_action = evaluation.action
                if eval_action == Action.DENY:
                    raw_action = Action.DENY
                    deny_would_fire = True
                elif eval_action == Action.ESCALATE and raw_action != Action.DENY:
                    raw_action = Action.ESCALATE
                elif eval_action == Action.AUDIT and raw_action == Action.ALLOW:
                    raw_action = Action.AUDIT

        # Apply enforcement mode
        final_action = self._apply_enforcement_mode(raw_action)

        # Build metadata with mode info
        record_metadata = dict(context.metadata)
        record_metadata["enforcement_mode"] = mode.value
        if deny_would_fire and self.is_audit_mode():
            record_metadata["audit_mode_would_deny"] = True

        audit = AuditRecord(
            timestamp=datetime.now(timezone.utc),
            agent_name=context.agent_name,
            runtime_id=context.runtime_id,
            hook=context.hook,
            evaluations=evaluations,
            final_action=final_action,
            metadata=record_metadata,
        )

        self._emit_audit(audit, mode)

        # For any guardrail mapped to UiPath but currently disabled,
        # dispatch a compensating-governance call via the injected
        # compensator. The compensating call (provider-owned) runs the
        # real guardrail check and writes its own audit trace — the
        # evaluator does NOT emit a Python-side trace for these rules,
        # to avoid double-writing. Fire-and-forget so a slow downstream
        # never blocks the agent hook.
        self._dispatch_compensation(audit, context)

        if final_action == Action.DENY:
            raise GovernanceBlockException.from_audit_record(audit)

        return audit

    def _dispatch_compensation(
        self, audit: AuditRecord, context: CheckContext
    ) -> None:
        """Schedule compensating governance for any matched fallback rules.

        Delegates to the injected :class:`GuardrailCompensator`. The
        compensator owns concurrency, queue caps, exception isolation,
        and graceful process-exit cancellation — this method just
        builds the payload, logs the summary, and submits.

        No-op when no compensator was supplied at construction (e.g.
        unit tests that don't care about the dispatch path).
        """
        if self._compensator is None:
            return

        try:
            disabled = disabled_guardrails(audit, self._policy_index)
            if not disabled:
                return

            # Distinct validator names for the operator-facing log line.
            validators = [rule.validator for rule in disabled]

            # Surface the disabled-guardrail fire-up: how many rules
            # triggered the compensating call, and which validators
            # they map to (e.g. pii_detection / prompt_injection /
            # harmful_content). One line per dispatch so an operator
            # can see the volume + breakdown at a glance.
            logger.info(
                "Compensating governance triggered: hook=%s, count=%d, validators=[%s]",
                audit.hook.value,
                len(disabled),
                ", ".join(validators),
            )

            self._compensator.submit(
                rules=disabled,
                data=_compensation_data_for_hook(context),
                hook=audit.hook.value,
                src_timestamp=audit.timestamp.isoformat(),
                agent_name=audit.agent_name,
                runtime_id=audit.runtime_id,
            )
        except Exception as exc:  # noqa: BLE001 - fail-open
            logger.warning(
                "Failed to dispatch compensating governance call: %s", exc
            )

    def _emit_audit(self, audit: AuditRecord, mode: EnforcementMode) -> None:
        """Emit per-rule and hook-summary events to the injected audit manager.

        No-op when no audit manager was supplied at construction. The
        per-runtime :class:`AuditManager` handles sink-level circuit
        breaking; emission errors stay there and never break evaluation.
        """
        manager = self._audit_manager
        if manager is None:
            return

        hook_name = audit.hook.name

        # ``guardrail_fallback`` rules are traced by the compensating
        # path (see :meth:`_dispatch_compensation`), which carries the
        # actual validator verdict. Emitting a Python-side
        # ``rule_evaluation`` event here would produce a duplicate
        # trace carrying no verdict, so filter these rules out of every
        # event the evaluator emits (per-rule AND the hook summary).
        emittable = [
            ev for ev in audit.evaluations
            if not self._is_guardrail_fallback_rule(ev.rule_id)
        ]

        # No emittable rules means every match this hook was a
        # guardrail-fallback already traced by the compensation path.
        # Skip the hook summary entirely — emitting it with
        # total_rules=0 + the audit's overall final_action would
        # double-count the compensation-owned verdict.
        if not emittable:
            return

        for evaluation in emittable:
            manager.emit_rule_evaluation(
                policy_id=evaluation.rule_id,
                rule_name=evaluation.rule_name,
                pack_name=evaluation.pack_name,
                hook=hook_name,
                matched=evaluation.matched,
                action=evaluation.action.value if evaluation.matched else "allow",
                enforcement_mode=mode,
                detail=evaluation.detail,
                agent_name=audit.agent_name,
                description=evaluation.description,
            )

        # Derive the summary's final action from the emittable subset
        # only. ``audit.final_action`` folded in fallback rules whose
        # verdict travels on the compensation path; including them
        # here would mix the two trace paths' verdicts.
        summary_final_action = self._apply_enforcement_mode(
            self._most_restrictive_matched_action(emittable)
        )

        manager.emit_hook_summary(
            hook=hook_name,
            agent_name=audit.agent_name,
            total_rules=len(emittable),
            matched_rules=sum(1 for ev in emittable if ev.matched),
            final_action=summary_final_action.value,
            enforcement_mode=mode,
        )

    @staticmethod
    def _most_restrictive_matched_action(
        evals: list[RuleEvaluation],
    ) -> Action:
        """Return the most-restrictive matched action across ``evals``.

        Mirrors the cross-rule aggregation in :meth:`evaluate`:
        DENY > ESCALATE > AUDIT > ALLOW. Unmatched rules contribute
        nothing.
        """
        result = Action.ALLOW
        for ev in evals:
            if not ev.matched:
                continue
            a = ev.action
            if a == Action.DENY:
                return Action.DENY
            if a == Action.ESCALATE and result != Action.DENY:
                result = Action.ESCALATE
            elif a == Action.AUDIT and result == Action.ALLOW:
                result = Action.AUDIT
        return result

    def _is_guardrail_fallback_rule(self, rule_id: str) -> bool:
        """Return True if the rule carries a ``guardrail_fallback`` condition.

        Such rules are traced by the compensating path, so the
        evaluator must not emit a duplicate Python-side trace for them.
        """
        rule = self._policy_index.get_rule(rule_id)
        if rule is None:
            return False
        for check in rule.checks:
            for cond in check.conditions:
                if cond.operator == "guardrail_fallback":
                    return True
        return False

    def _apply_enforcement_mode(self, raw_action: Action) -> Action:
        """Apply enforcement mode to the raw action.

        In AUDIT mode:
        - DENY becomes AUDIT (log but don't block)
        - ESCALATE becomes AUDIT (log but don't escalate)
        - AUDIT stays AUDIT
        - ALLOW stays ALLOW

        In ENFORCE mode:
        - All actions pass through unchanged
        """
        if self._enforcement_mode == EnforcementMode.AUDIT:
            if raw_action in (Action.DENY, Action.ESCALATE):
                return Action.AUDIT
        return raw_action

    def evaluate_before_agent(
        self,
        agent_input: str,
        agent_name: str,
        runtime_id: str,
        model_name: str = "",
        **kwargs: Any,
    ) -> AuditRecord:
        """Evaluate BEFORE_AGENT rules."""
        context = CheckContext(
            hook=LifecycleHook.BEFORE_AGENT,
            agent_name=agent_name,
            runtime_id=runtime_id,
            agent_input=agent_input,
            model_name=model_name,
            metadata=kwargs.get("metadata", {}),
        )
        return self.evaluate(context)

    def evaluate_after_agent(
        self,
        agent_output: str,
        agent_name: str,
        runtime_id: str,
        **kwargs: Any,
    ) -> AuditRecord:
        """Evaluate AFTER_AGENT rules."""
        context = CheckContext(
            hook=LifecycleHook.AFTER_AGENT,
            agent_name=agent_name,
            runtime_id=runtime_id,
            agent_output=agent_output,
            metadata=kwargs.get("metadata", {}),
        )
        return self.evaluate(context)

    def evaluate_before_model(
        self,
        model_input: str,
        agent_name: str,
        runtime_id: str,
        messages: list[dict[str, Any]] | None = None,
        model_name: str = "",
        **kwargs: Any,
    ) -> AuditRecord:
        """Evaluate BEFORE_MODEL rules."""
        context = CheckContext(
            hook=LifecycleHook.BEFORE_MODEL,
            agent_name=agent_name,
            runtime_id=runtime_id,
            model_input=model_input,
            model_name=model_name,
            messages=messages or [],
            metadata=kwargs.get("metadata", {}),
        )
        return self.evaluate(context)

    def evaluate_after_model(
        self,
        model_output: str,
        agent_name: str,
        runtime_id: str,
        **kwargs: Any,
    ) -> AuditRecord:
        """Evaluate AFTER_MODEL rules."""
        context = CheckContext(
            hook=LifecycleHook.AFTER_MODEL,
            agent_name=agent_name,
            runtime_id=runtime_id,
            model_output=model_output,
            metadata=kwargs.get("metadata", {}),
        )
        return self.evaluate(context)

    def evaluate_tool_call(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        agent_name: str,
        runtime_id: str,
        session_state: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> AuditRecord:
        """Evaluate TOOL_CALL rules."""
        context = CheckContext(
            hook=LifecycleHook.TOOL_CALL,
            agent_name=agent_name,
            runtime_id=runtime_id,
            tool_name=tool_name,
            tool_args=tool_args,
            session_state=session_state or {},
            metadata=kwargs.get("metadata", {}),
        )
        return self.evaluate(context)

    def evaluate_after_tool(
        self,
        tool_name: str,
        tool_result: str,
        agent_name: str,
        runtime_id: str,
        **kwargs: Any,
    ) -> AuditRecord:
        """Evaluate AFTER_TOOL rules."""
        context = CheckContext(
            hook=LifecycleHook.AFTER_TOOL,
            agent_name=agent_name,
            runtime_id=runtime_id,
            tool_name=tool_name,
            tool_result=tool_result,
            metadata=kwargs.get("metadata", {}),
        )
        return self.evaluate(context)

    def _evaluate_rule(self, rule: Rule, context: CheckContext) -> RuleEvaluation:
        """Evaluate a single rule against the context."""
        if not rule.checks:
            # No checks = always matches (for audit-only rules)
            return RuleEvaluation(
                rule_id=rule.rule_id,
                rule_name=rule.name,
                matched=True,
                detail="Rule has no conditions (always matches)",
                pack_name=rule.pack_name,
                action=rule.action,
                description=rule.description,
            )

        check_results: list[dict[str, Any]] = []
        any_check_matched = False
        # Resolve the rule's action from the MATCHED checks so per-check
        # `action` overrides take effect. ``Check.action`` defaults to the
        # rule's action (see _yaml_to_index), so for rules without an
        # override this equals ``rule.action`` exactly. Take the most
        # restrictive matched action (DENY > ESCALATE > AUDIT > ALLOW),
        # mirroring evaluate()'s cross-rule aggregation.
        matched_action = Action.ALLOW

        for check in rule.checks:
            matched, detail = self._evaluate_check(check, context)
            check_results.append(
                {
                    "matched": matched,
                    "detail": detail,
                    "action": check.action.value,
                }
            )
            if matched:
                any_check_matched = True
                if check.action == Action.DENY:
                    matched_action = Action.DENY
                elif (
                    check.action == Action.ESCALATE
                    and matched_action != Action.DENY
                ):
                    matched_action = Action.ESCALATE
                elif (
                    check.action == Action.AUDIT
                    and matched_action == Action.ALLOW
                ):
                    matched_action = Action.AUDIT

        # Surface the FIRST matched check's message; falls back to the
        # first check's detail (empty string when none matched) for
        # backward compatibility with rules that have a single check.
        first_matched_detail = next(
            (cr["detail"] for cr in check_results if cr["matched"]),
            check_results[0]["detail"] if check_results else "",
        )

        return RuleEvaluation(
            rule_id=rule.rule_id,
            rule_name=rule.name,
            matched=any_check_matched,
            detail=first_matched_detail,
            pack_name=rule.pack_name,
            action=matched_action if any_check_matched else Action.ALLOW,
            description=rule.description,
            check_results=check_results,
        )

    def _evaluate_check(self, check: Check, context: CheckContext) -> tuple[bool, str]:
        """Evaluate a single check against the context."""
        if not check.conditions:
            return True, "No conditions (always matches)"

        results = []
        for condition in check.conditions:
            matched = self._evaluate_condition(condition, context)
            results.append(matched)

        if check.logic == "any":
            final_match = any(results)
        else:  # "all" is default
            final_match = all(results)

        detail = check.message if final_match else ""
        return final_match, detail

    def _evaluate_condition(self, condition: Condition, context: CheckContext) -> bool:
        """Evaluate a single condition against the context."""
        field_value = self._get_field_value(condition.field, context)
        result = self._apply_operator(condition.operator, field_value, condition.value)

        if condition.negate:
            result = not result

        return result

    def _get_field_value(self, field: str, context: CheckContext) -> Any:
        """Get a field value from the context."""
        parts = field.split(".")

        # Start with context
        value: Any = context

        for part in parts:
            if hasattr(value, part):
                value = getattr(value, part)
            elif isinstance(value, dict) and part in value:
                value = value[part]
            else:
                return None

        return value

    def _apply_operator(
        self, operator: str, field_value: Any, check_value: Any
    ) -> bool:
        """Apply an operator to compare field value against check value."""
        # Handle existence checks before the None check
        if operator == "exists":
            return field_value is not None
        if operator == "not_exists":
            return field_value is None

        # guardrail_fallback fires only when the guardrail is mapped to
        # UiPath but its policy is disabled. Config travels in
        # ``check_value``; the rule's ``field`` is unused (so
        # ``field_value`` is ``None`` here, which is expected — we must
        # special-case this before the generic ``None`` short-circuit
        # below).
        if operator == "guardrail_fallback":
            cfg = check_value if isinstance(check_value, dict) else {}
            return bool(cfg.get("mapped_to_uipath", False)) and not bool(
                cfg.get("policy_enabled", True)
            )

        if field_value is None:
            return False

        # Numeric operators don't need stringification — short-circuit
        # before `str(field_value)` (expensive for dict / large payloads).
        if operator in ("gt", "gte", "lt", "lte"):
            try:
                lhs = float(field_value)
                rhs = float(check_value)
            except (ValueError, TypeError):
                return False
            if operator == "gt":
                return lhs > rhs
            if operator == "gte":
                return lhs >= rhs
            if operator == "lt":
                return lhs < rhs
            return lhs <= rhs

        field_str = str(field_value)

        match operator:
            case "equals" | "eq":
                return field_str == str(check_value)

            case "not_equals" | "ne":
                return field_str != str(check_value)

            case "contains":
                return str(check_value).lower() in field_str.lower()

            case "not_contains":
                return str(check_value).lower() not in field_str.lower()

            case "regex" | "matches":
                compiled = _compile_regex(str(check_value))
                if compiled is None:
                    return False
                return bool(compiled.search(field_str))

            case "in_list":
                if isinstance(check_value, list):
                    return field_str in check_value
                return False

            case "not_in_list":
                if isinstance(check_value, list):
                    return field_str not in check_value
                return True

            case "vader_concern":
                # VADER compound score <= threshold.
                # check_value: dict like {"threshold": -0.3} (default -0.3)
                return self._check_vader_concern(field_str, check_value)

            case "encoding_concern":
                # chardet-backed encoding integrity check (A.7.4).
                # check_value: dict with optional `min_confidence` (default 0.5)
                # and `max_replacement_ratio` (default 0.05).
                return self._check_encoding_concern(field_str, check_value)

            case "entropy_concern":
                # Shannon entropy outside expected range (A.7.4).
                # check_value: dict with optional `min` (default 1.5) and
                # `max` (default 7.5) bits/byte. Stdlib only.
                return self._check_entropy_concern(field_str, check_value)

            case "incident_concern":
                # Categorical incident detection (A.8.4).
                # check_value: dict with optional `categories` list
                # (subset of safety_refusal/tool_failure/auth_failure/
                # quota_exceeded/hallucination). Default: all categories.
                return self._check_incident_concern(field_str, check_value)

            case "commitment_concern":
                # Customer commitment language detection (A.10.4).
                # check_value: dict with optional `require_amount` (default
                # True) and `require_deadline` (default False). Fires when
                # a commitment verb co-occurs with the configured signals.
                return self._check_commitment_concern(field_str, check_value)

            case _:
                logger.debug("Unknown operator: %s", operator)
                return False

    @staticmethod
    def _check_vader_concern(text: str, params: Any) -> bool:
        """Return True if VADER compound score on `text` is <= threshold.

        Args:
            text: Text to analyse.
            params: Either a dict with `threshold` key, or a numeric threshold
                directly. Default threshold is -0.3 (clearly-negative).

        Returns:
            True iff vaderSentiment is available AND compound score <= threshold.
            Returns False on empty input or if the library is not installed —
            sentiment checks no-op rather than crash.
        """
        if not text or not text.strip():
            return False

        analyzer = _get_vader_analyzer()
        if analyzer is None:
            return False

        if isinstance(params, dict):
            threshold = float(params.get("threshold", -0.3))
        else:
            try:
                threshold = float(params)
            except (TypeError, ValueError):
                threshold = -0.3

        try:
            compound = float(analyzer.polarity_scores(text)["compound"])
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("VADER analysis failed: %s", exc)
            return False

        return compound <= threshold

    @staticmethod
    def _check_encoding_concern(text: str, params: Any) -> bool:
        r"""Return True if `text` shows encoding integrity issues.

        Sums multiple deterministic corruption signals against text length:
          - U+FFFD replacement characters (already-decoded lossy text)
          - Literal ``�`` escape sequences carried through a JSON
            / repr layer rather than being decoded
          - Literal ``\xHH`` hex escapes (raw bytes leaked into a string)
          - Latin-1-as-UTF-8 mojibake bigrams (e.g. ``Ã©``, ``â€™``)
        If the corruption ratio exceeds ``max_replacement_ratio`` the
        check fires. chardet (when installed) is consulted as a
        secondary low-confidence signal.
        """
        if not text or not text.strip():
            return False

        if not isinstance(params, dict):
            params = {}
        min_confidence = float(params.get("min_confidence", 0.5))
        max_replacement_ratio = float(params.get("max_replacement_ratio", 0.05))
        min_corruption_events = int(params.get("min_corruption_events", 2))

        length = max(len(text), 1)

        replacement_chars = text.count("�")
        literal_ufffd_escapes = text.count("\\ufffd")
        hex_escapes = len(_HEX_ESCAPE_PATTERN.findall(text))
        mojibake_bigrams = sum(text.count(bigram) for bigram in _MOJIBAKE_BIGRAMS)

        # Absolute count of distinct corruption *events* (one per
        # U+FFFD, one per literal escape sequence, one per mojibake
        # bigram). Even diluted by a lot of clean text, a few of these
        # in production output is a strong signal.
        corruption_events = (
            replacement_chars + literal_ufffd_escapes + hex_escapes + mojibake_bigrams
        )
        if corruption_events >= min_corruption_events:
            return True

        # Ratio-based fallback for cases below the absolute floor: still
        # catches very short payloads where a single corruption char is
        # disproportionate.
        # Weight each event by its source-char span so denser corruption
        # in shorter text trips the ratio sooner:
        #   U+FFFD = 1 char, "�" = 6 chars, "\xHH" = 4 chars,
        #   mojibake bigram = 2 chars.
        corruption_chars = (
            replacement_chars
            + 6 * literal_ufffd_escapes
            + 4 * hex_escapes
            + 2 * mojibake_bigrams
        )
        if corruption_chars / length > max_replacement_ratio:
            return True

        # Secondary: chardet on the encoded bytes. For pure str input
        # this almost always reports high UTF-8/ASCII confidence (the
        # branch is intentionally permissive), but it does catch bytes
        # routed through `repr()` or `__str__` of a `bytes` object that
        # chardet recognises as a non-UTF8 encoding with low confidence.
        chardet = _get_chardet()
        if chardet is None:
            return False
        try:
            detection = chardet.detect(text.encode("utf-8", errors="replace"))
            confidence = float(detection.get("confidence") or 0.0)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("chardet detection failed: %s", exc)
            return False

        return confidence < min_confidence

    @staticmethod
    def _check_entropy_concern(text: str, params: Any) -> bool:
        """Return True if Shannon entropy of `text` is outside an expected range.

        Stdlib-only. Entropy is computed in bits per symbol over byte
        frequencies. English prose typically lands ~3.5–4.5 bits/byte;
        binary noise approaches 8 bits/byte; constant/repetitive text
        approaches 0.
        """
        if not text or not text.strip():
            return False

        if not isinstance(params, dict):
            params = {}
        lo = float(params.get("min", 1.5))
        hi = float(params.get("max", 7.5))

        data = text.encode("utf-8", errors="replace")
        total = len(data)
        if total == 0:
            return False

        counts = Counter(data)
        entropy = 0.0
        for c in counts.values():
            p = c / total
            entropy -= p * math.log2(p)

        return entropy < lo or entropy > hi

    @staticmethod
    def _check_incident_concern(text: str, params: Any) -> bool:
        """Return True if `text` matches any configured incident pattern (A.8.4).

        Categories: safety_refusal, tool_failure, auth_failure,
        quota_exceeded, hallucination. Pass ``{"categories": [...]}`` to
        restrict; default scans all categories.
        """
        if not text or not text.strip():
            return False

        if isinstance(params, dict):
            requested = params.get("categories")
        else:
            requested = None

        if not requested:
            categories = list(_INCIDENT_PATTERNS.keys())
        else:
            categories = [c for c in requested if c in _INCIDENT_PATTERNS]

        for category in categories:
            for pattern in _INCIDENT_PATTERNS[category]:
                if pattern.search(text):
                    return True
        return False

    @staticmethod
    def _check_commitment_concern(text: str, params: Any) -> bool:
        """Return True if `text` carries customer-commitment language (A.10.4).

        OR semantics: a commitment-verb match always fires; when
        ``require_amount`` is true, a currency-anchored amount alone also
        fires; when ``require_deadline`` is true, a deadline phrase alone
        also fires. With both flags false the rule matches on verb only
        (verb-only mode).

        The verb pattern covers first-person promise verbs *and* proposal
        / SOW commitment markers ("Cost: $X", "fixed scope",
        "Deliverables", "Timeline: N days", "I propose"). The amount
        pattern requires a currency marker adjacent to the number so URL
        fragments don't false-positive.
        """
        if not text or not text.strip():
            return False

        if not isinstance(params, dict):
            params = {}
        require_amount = bool(params.get("require_amount", True))
        require_deadline = bool(params.get("require_deadline", False))

        verb_match = bool(_COMMITMENT_VERB_PATTERN.search(text))

        # Verb-only mode: neither supporting signal is enabled.
        if not require_amount and not require_deadline:
            return verb_match

        amount_match = require_amount and bool(
            _COMMITMENT_AMOUNT_FALLBACK.search(text)
        )
        deadline_match = require_deadline and bool(
            _COMMITMENT_DEADLINE_PATTERN.search(text)
        )
        return verb_match or amount_match or deadline_match
