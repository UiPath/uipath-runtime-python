"""Compensating governance for disabled centralized guardrails.

When a ``guardrail_fallback`` rule fires (the guardrail is mapped to
UiPath but the centralized policy is disabled), the framework asks the
governance-server to run the real guardrail check via its
``/{org_id}/agenticgovernance_/api/v1/runtime/govern`` endpoint.

This module owns only the **local concerns**: a bounded background
pool that schedules the call without blocking the agent hook, and a
trace-id capture that runs on the caller thread before the worker hop
(the worker has no OpenTelemetry context).

The actual HTTP call — URL composition, auth, headers, JSON
serialisation, env-backed job-context auto-fill — is the
:class:`uipath.core.governance.GovernanceCompensationProvider`'s job.
Callers inject a concrete provider implementation, and this module
just builds the :class:`GovernRequest` wire model and hands it off.

The call is **fire-and-forget**: the server runs the guardrail AND
writes the audit trace from its side. The agent doesn't inspect the
response — it only cares about whether the call reached the server.

The compensator is **instance-scoped**: each :class:`GovernanceRuntime`
owns its own pool and semaphore. ``uipath eval`` parallel runtimes
don't share workers, queue slots, or saturation state — one runtime's
spam can't silently drop another's compensation calls.

The compensator does **not** read host env vars and does not resolve
trace ids itself. It propagates the caller's ``contextvars`` (which
hold the live OTel span) across the worker-thread hop via
:func:`contextvars.copy_context`, so the provider can resolve trace
context at HTTP-call time inside the captured context.
"""

from __future__ import annotations

import atexit
import contextvars
import logging
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from uipath.core.governance import (
    FiredRule,
    GovernanceCompensationProvider,
    GovernRequest,
)

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# Process-wide cleanup machinery
#
# One ``atexit`` hook walks a ``WeakSet`` of live compensators on exit and
# closes each. Bounded atexit registrations (N runtimes → 1 hook, not N) and
# weakref tracking so a disposed compensator can be GC'd. Same pattern as
# :class:`uipath.runtime.governance._audit.base.AuditManager`.
# ----------------------------------------------------------------------------

_live_compensators: weakref.WeakSet[GuardrailCompensator] = weakref.WeakSet()
_atexit_registered = False
_atexit_lock = threading.Lock()


def _process_cleanup_compensators() -> None:
    """Process-exit handler: close every live compensator."""
    for compensator in list(_live_compensators):
        try:
            compensator.close()
        except Exception as exc:  # noqa: BLE001 - exit cleanup must not raise
            logger.debug("Compensator process cleanup error: %s", exc)


def _register_compensator_for_cleanup(compensator: GuardrailCompensator) -> None:
    """Add ``compensator`` to the cleanup set + ensure atexit is wired once."""
    global _atexit_registered
    _live_compensators.add(compensator)
    if _atexit_registered:
        return
    with _atexit_lock:
        if not _atexit_registered:
            atexit.register(_process_cleanup_compensators)
            _atexit_registered = True


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
    """Instance-scoped compensating-governance dispatcher.

    Each :class:`GovernanceRuntime` constructs one. Owns:

    - A :class:`ThreadPoolExecutor` (default 4 workers) that runs the
      ``/runtime/govern`` POST off the agent's hook thread.
    - A :class:`threading.BoundedSemaphore` (default cap = workers × 4)
      that bounds total in-flight submissions (running + queued) so a
      misbehaving agent firing compensation faster than the server can
      absorb can't grow memory without limit. Saturated submissions are
      dropped with a warning.

    Process exit cancels queued work via a single process-level atexit
    handler (see :func:`_process_cleanup_compensators`); running tasks
    finish bounded by the provider's HTTP timeout.

    Fire-and-forget: :meth:`submit` returns immediately. The actual HTTP
    work is delegated to :meth:`GovernanceCompensationProvider.compensate`
    — this class never touches URL/headers/auth/JSON itself.
    """

    _DEFAULT_MAX_WORKERS = 4
    # Queue depth multiplier — total in-flight cap = max_workers × this.
    _INFLIGHT_OVERSUBSCRIPTION = 4

    def __init__(
        self,
        provider: GovernanceCompensationProvider,
        *,
        max_workers: int = _DEFAULT_MAX_WORKERS,
        inflight_oversubscription: int = _INFLIGHT_OVERSUBSCRIPTION,
    ) -> None:
        """Construct a compensator bound to one provider.

        The compensator does not carry a trace id. Trace-id resolution
        is the provider's responsibility at HTTP-call time. To preserve
        live OTel context across the thread-pool hop (worker threads
        don't inherit ``contextvars``), :meth:`submit` runs the worker
        callable inside a snapshot captured via
        :func:`contextvars.copy_context` — so the caller's OTel span is
        still visible when the provider runs on the worker.

        Args:
            provider: The :class:`GovernanceCompensationProvider` that
                actually fires the ``/runtime/govern`` POST.
            max_workers: Concurrent worker threads in the pool.
            inflight_oversubscription: How deep the work queue grows
                before saturated submissions get dropped. Total cap is
                ``max_workers * inflight_oversubscription``.
        """
        self._provider = provider
        self._inflight_cap = max_workers * inflight_oversubscription
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="governance-compensation",
        )
        self._inflight = threading.BoundedSemaphore(self._inflight_cap)
        _register_compensator_for_cleanup(self)

    def submit(
        self,
        rules: list[FiredRule],
        data: dict[str, Any],
        hook: str,
        src_timestamp: str,
        agent_name: str,
        runtime_id: str,
    ) -> None:
        """Schedule a /runtime/govern call on the bounded background pool.

        Fire-and-forget. Returns immediately; the call runs on a worker
        thread. When the in-flight queue is saturated the call is
        dropped with a warning and the agent continues.

        ``rules`` is the per-rule metadata from :func:`disabled_guardrails`;
        the validators sent to the guardrail API are derived from it.

        The current :mod:`contextvars` context (which carries the live
        OpenTelemetry span) is captured here and re-applied inside the
        worker via :meth:`contextvars.Context.run`. This lets the
        provider see the live OTel context on the worker thread —
        without the snapshot the worker would inherit an empty context
        and the provider could only resolve env-based trace ids.

        Never raises — including when the pool has already been shut down.
        """
        if not rules:
            return

        validators = _validators(rules)
        if not validators:
            return

        if not self._inflight.acquire(blocking=False):
            logger.warning(
                "Compensation pool saturated (>%d in flight); dropping call "
                "(validators=[%s])",
                self._inflight_cap,
                ", ".join(validators),
            )
            return

        request = GovernRequest(
            validators=validators,
            rules=rules,
            data=data,
            hook=hook,
            src_timestamp=src_timestamp,
            agent_name=agent_name,
            runtime_id=runtime_id,
        )

        provider = self._provider
        inflight = self._inflight
        # Snapshot the caller's contextvars (OTel span lives in there
        # for Python OTel >= 1.x). The worker runs inside this snapshot
        # so the provider sees the live span at HTTP-call time.
        ctx = contextvars.copy_context()

        def _run() -> None:
            try:
                provider.compensate(request)
            except Exception as exc:  # noqa: BLE001 - fail-open by contract
                logger.warning(
                    "Compensation worker failed (validators=[%s]): %s",
                    ", ".join(validators),
                    exc,
                )
            finally:
                inflight.release()

        try:
            self._pool.submit(ctx.run, _run)
        except RuntimeError as exc:
            # Pool was shut down (atexit, dispose, or test teardown) —
            # release the semaphore slot we took and log; never raise.
            self._inflight.release()
            logger.warning(
                "Compensation pool unavailable (validators=[%s]): %s",
                ", ".join(validators),
                exc,
            )

    def close(self) -> None:
        """Cancel queued tasks. Running tasks finish bounded by the provider HTTP timeout.

        ``wait=False`` returns immediately so caller / process shutdown
        isn't held up; ``cancel_futures=True`` drops anything not yet
        running. Idempotent — calling close on an already-closed pool
        is a logged no-op.
        """
        try:
            self._pool.shutdown(wait=False, cancel_futures=True)
        except Exception as exc:  # noqa: BLE001 - shutdown must not raise
            logger.debug("Compensator shutdown error: %s", exc)
