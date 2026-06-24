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
Callers inject a concrete provider (typically
``uipath.platform.governance.UiPathPlatformGovernanceProvider``) and
this module just builds the :class:`GovernRequest` wire model and hands
it off.

The call is **fire-and-forget**: the server runs the guardrail AND
writes the audit trace from its side. The agent doesn't inspect the
response — it only cares about whether the call reached the server.

The call also runs on a **bounded background pool** so even an agent
that fires hundreds of compensation events in a session can't pile up
threads or memory. :data:`COMPENSATION_MAX_WORKERS` workers process
the queue, and an in-flight semaphore drops submissions when the pool
is genuinely saturated — at that point the next call is logged and
skipped rather than queued indefinitely.
"""

from __future__ import annotations

import atexit
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from uipath.core.governance import (
    FiredRule,
    GovernanceCompensationProvider,
    GovernRequest,
)

logger = logging.getLogger(__name__)

# Trace-id env var published by the UiPath runtime host. Native governance
# audit spans are exported under this id (the platform rebinds spans to the
# agent's run trace), so server-written compensation records must land on
# the same id — see :func:`_resolve_trace_id`.
ENV_TRACE_ID = "UIPATH_TRACE_ID"

# Max concurrent workers in the compensation pool. Compensation is
# fire-and-forget I/O bounded by the provider's HTTP timeout, so a small
# fixed pool is enough; the in-flight semaphore (workers × oversubscription)
# is what really bounds memory under load.
COMPENSATION_MAX_WORKERS = 4


# ----------------------------------------------------------------------------
# Bounded thread pool — caps both concurrent threads AND queued work.
#
# ThreadPoolExecutor alone caps concurrent worker threads, but its internal
# queue is unbounded — a misbehaving agent that fires compensation faster than
# the server can absorb would queue indefinitely (memory pressure). The
# semaphore caps total in-flight submissions (running + queued) at a
# multiple of the worker count. Saturated submissions are dropped with a
# warning. Process exit cancels queued work and lets running tasks finish
# (bounded by the provider's HTTP timeout) via the atexit handler.
# ----------------------------------------------------------------------------

_INFLIGHT_OVERSUBSCRIPTION = 4  # queue up to (workers × this many) before dropping
_INFLIGHT_CAP = COMPENSATION_MAX_WORKERS * _INFLIGHT_OVERSUBSCRIPTION

_pool = ThreadPoolExecutor(
    max_workers=COMPENSATION_MAX_WORKERS,
    thread_name_prefix="governance-compensation",
)
_inflight = threading.BoundedSemaphore(_INFLIGHT_CAP)


@atexit.register
def _shutdown_pool() -> None:
    """Cancel queued compensation tasks at process exit.

    ``wait=False`` returns immediately so process shutdown isn't held
    up; ``cancel_futures=True`` (Python 3.9+) drops anything not yet
    running. Tasks already running finish bounded by the provider's
    own HTTP timeout.
    """
    try:
        _pool.shutdown(wait=False, cancel_futures=True)
    except Exception:  # noqa: BLE001 - shutdown must never raise from atexit
        pass


# ----------------------------------------------------------------------------
# Public API
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


def _resolve_trace_id(fallback: str) -> str:
    """Resolve the agent's trace id while still on the caller thread.

    MUST be called before the background-pool hop in
    :func:`submit_compensation`: the worker thread that issues the
    ``/govern`` call has no OpenTelemetry context, so resolving there would
    fall back to a detached id — orphaning the server-written compensation
    records from the agent's real trace.

    Order: ``UIPATH_TRACE_ID`` env var -> live OTel span trace id
    (32-char hex) -> the caller-supplied ``fallback``.

    ``UIPATH_TRACE_ID`` is preferred over the live OTel span because the
    native governance audit spans are exported under that id (the platform
    rebinds spans to the agent's run trace). The compensation records must
    land on the *same* trace, so we use it first. The live OTel span is the
    fallback for contexts where the env var isn't set; in conversational
    runs the hook thread has no live span anyway, so the env var is what
    keeps native + compensation on one trace.
    """
    env_trace_id = os.environ.get(ENV_TRACE_ID)
    if env_trace_id:
        return env_trace_id

    try:
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        if ctx.is_valid:
            return format(ctx.trace_id, "032x")
    except Exception:  # noqa: BLE001 - tracing is best-effort; fall through
        pass

    return fallback


def submit_compensation(
    provider: GovernanceCompensationProvider,
    rules: list[FiredRule],
    data: dict[str, Any],
    hook: str,
    trace_id: str,
    src_timestamp: str,
    agent_name: str,
    runtime_id: str,
) -> None:
    """Schedule a /runtime/govern call on the bounded background pool.

    Fire-and-forget. Returns immediately; the call runs on a worker
    thread bounded by :data:`COMPENSATION_MAX_WORKERS`. When the
    in-flight queue is saturated (cap = workers × oversubscription),
    the call is dropped with a warning and the agent continues.

    The actual HTTP work is delegated to ``provider.compensate(request)``
    where ``request`` is a :class:`GovernRequest`. The provider owns URL
    composition, auth, headers, JSON serialisation, and env-backed
    auto-fill of job-context fields (``folder_key`` / ``job_key`` /
    ``process_key`` / ``reference_id`` / ``agent_version``) — this module
    only assembles the wire model and schedules the call.

    ``rules`` is the per-rule metadata from :func:`disabled_guardrails`;
    the validators sent to the guardrail API are derived from it.

    Never raises — including when the pool has already been shut down
    by process exit.
    """
    if not rules:
        return

    validators = _validators(rules)
    if not validators:
        return

    # Resolve the trace id HERE, on the caller (hook) thread where the
    # agent's OTel span is still live. The provider.compensate call below
    # runs on a background worker where that context is gone, so the
    # resolved value is captured now and carried into the worker —
    # ensuring the server writes compensation records under the agent's
    # real trace, not a detached id.
    trace_id = _resolve_trace_id(trace_id)

    if not _inflight.acquire(blocking=False):
        logger.warning(
            "Compensation pool saturated (>%d in flight); dropping call "
            "(validators=[%s])",
            _INFLIGHT_CAP,
            ", ".join(validators),
        )
        return

    request = GovernRequest(
        validators=validators,
        rules=rules,
        data=data,
        hook=hook,
        trace_id=trace_id,
        src_timestamp=src_timestamp,
        agent_name=agent_name,
        runtime_id=runtime_id,
    )

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
            _inflight.release()

    try:
        _pool.submit(_run)
    except RuntimeError as exc:
        # Pool was shut down (atexit or test teardown) — release the
        # semaphore slot we took and log; never raise.
        _inflight.release()
        logger.warning(
            "Compensation pool unavailable (validators=[%s]): %s",
            ", ".join(validators),
            exc,
        )
