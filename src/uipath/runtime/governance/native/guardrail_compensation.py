"""Compensating governance for disabled centralized guardrails.

When a ``guardrail_fallback`` rule fires, this module builds the
:class:`GovernRequest` wire payload and calls the injected
:class:`uipath.core.governance.GovernanceCompensationProvider`. The
provider runs the actual guardrail check and writes its own audit
trace; the runtime layer here only assembles the request.

Synchronous dispatch on the caller's thread. The provider owns
non-blocking semantics (internal batching, async fire-and-forget, or
whatever scheduling the host wires) — the runtime layer no longer
holds a worker pool, semaphore, or process-exit cleanup for this
path. Same architectural shape as :class:`AuditManager`: runtime is
synchronous and pure; async export is the sink/provider's concern.
"""

from __future__ import annotations

import logging
from typing import Any

from uipath.core.governance import (
    FiredRule,
    GovernanceCompensationProvider,
    GovernRequest,
)

from .._audit.metadata import GovernanceRuntimeMetadata

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# Stateless helpers
# ----------------------------------------------------------------------------


def disabled_guardrails(audit: Any, policy_index: Any) -> list[FiredRule]:
    """Return per-rule metadata for each fired guardrail-fallback rule.

    A guardrail rule fires only when it is mapped to UiPath
    (``mapped_to_uipath`` true) but disabled (``policy_enabled`` false) —
    see the ``guardrail_fallback`` operator. The validator name (e.g.
    ``pii_detection``) is read from the rule's ``guardrail_fallback``
    check config and used as the validator on the compensating call.

    One :class:`FiredRule` entry is emitted per matching
    ``guardrail_fallback`` condition. Rules in this codebase declare a
    single fallback condition each, so the returned list has one entry
    per fired rule in practice; multi-condition rules would emit more
    than one entry sharing the same ``rule_id``.
    """
    out: list[FiredRule] = []
    for ev in audit.evaluations:
        if not ev.matched:
            continue
        rule = policy_index.get_rule(ev.rule_id)
        if rule is None:
            continue
        for check in rule.checks:
            for cond in check.conditions:
                if cond.operator != "guardrail_fallback":
                    continue
                if not isinstance(cond.value, dict):
                    continue
                # The ``guardrail_fallback`` operator at evaluation time
                # only matches when ``mapped_to_uipath=True`` AND
                # ``policy_enabled=False``. We re-check here defensively
                # so a future code path that bypasses the evaluator (or
                # a multi-condition rule that fired on a sibling check)
                # can't trigger a compensation call for a guardrail
                # that isn't actually disabled.
                if not bool(cond.value.get("mapped_to_uipath", False)):
                    continue
                if bool(cond.value.get("policy_enabled", True)):
                    continue
                validator = str(cond.value.get("validator", ""))
                if validator:
                    out.append(
                        FiredRule(
                            rule_id=ev.rule_id,
                            rule_name=ev.rule_name,
                            pack_name=getattr(rule, "pack_name", "") or "",
                            validator=validator,
                        )
                    )
    return out


def _validators(rules: list[FiredRule]) -> list[str]:
    """Distinct validator names from the fired rules, preserving order."""
    return list(dict.fromkeys(r.validator for r in rules if r.validator))


# ----------------------------------------------------------------------------
# GuardrailCompensator
# ----------------------------------------------------------------------------


class GuardrailCompensator:
    """Synchronous dispatcher for compensating-governance calls.

    Builds the :class:`GovernRequest` payload and invokes the injected
    provider's ``compensate`` method on the caller's thread. The
    provider is expected to be non-blocking (batched internally, async
    fire-and-forget, or otherwise scheduled off the agent's hook
    thread) — the runtime layer owns no worker pool, semaphore, or
    process-exit cleanup for this path.

    Per-call exceptions are caught and logged so a provider failure
    never breaks the agent hook.
    """

    def __init__(
        self,
        provider: GovernanceCompensationProvider,
        *,
        runtime_metadata: GovernanceRuntimeMetadata | None = None,
    ) -> None:
        """Construct a compensator bound to one provider.

        Args:
            provider: Host-supplied
                :class:`GovernanceCompensationProvider`. ``compensate``
                is invoked synchronously on the agent's hook thread.
            runtime_metadata: Per-run identity (agent framework / type /
                runtime version) stamped onto the ``/runtime/govern`` call
                so the server can attach it to the rule-denied telemetry it
                emits. ``None`` leaves the fields unset (server defaults
                them to ``unknown``).
        """
        self._provider = provider
        self._runtime_metadata = runtime_metadata

    def submit(
        self,
        rules: list[FiredRule],
        data: dict[str, Any],
        hook: str,
        src_timestamp: str,
        agent_name: str,
        runtime_id: str,
    ) -> None:
        """Build the wire payload and hand it to the provider.

        Short-circuits on empty rules / empty validators. Per-call
        provider exceptions are caught and logged.
        """
        if not rules:
            return
        validators = _validators(rules)
        if not validators:
            return

        meta = self._runtime_metadata
        request = GovernRequest(
            validators=validators,
            rules=rules,
            data=data,
            hook=hook,
            src_timestamp=src_timestamp,
            agent_name=agent_name,
            runtime_id=runtime_id,
            agent_framework=meta.agent_framework if meta else None,
            agent_type=meta.agent_type if meta else None,
            runtime_version=meta.runtime_version if meta else None,
        )

        try:
            self._provider.compensate(request)
        except Exception as exc:  # noqa: BLE001 - fail-open by contract
            logger.warning(
                "Compensation provider call failed (validators=[%s]): %s",
                ", ".join(validators),
                exc,
            )

    def close(self) -> None:
        """No-op — the compensator holds no resources.

        Kept on the API so callers that wire ``close()`` (e.g. shared
        teardown patterns) don't need a branch for this class.
        """
