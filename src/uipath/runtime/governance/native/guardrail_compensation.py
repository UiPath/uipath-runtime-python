"""Compensating governance for disabled centralized guardrails.

When a ``guardrail_fallback`` rule fires (the guardrail is mapped to
UiPath but the centralized policy is disabled), the framework asks the
governance-server to run the real guardrail check via its
``/{org_id}/agenticgovernance_/api/v1/runtime/govern`` endpoint.

This call is **fire-and-forget**: the server runs the guardrail AND
writes the audit trace from its side. The agent doesn't inspect the
response — it only cares about whether the call reached the server.

The call also runs on a **bounded background pool** so even an agent
that fires hundreds of compensation events in a session can't pile up
threads or memory. :data:`COMPENSATION_MAX_WORKERS` workers process
the queue, and an in-flight semaphore drops submissions when the pool
is genuinely saturated — at that point the next call is logged and
skipped rather than queued indefinitely.

URL composition, request headers, org/tenant resolution, and the
request timeout all come from
:mod:`uipath.runtime.governance.native.backend_client` so the policy
fetch and the compensating call share one definition of every
operator-tunable.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypedDict

from uipath.runtime.governance.native.backend_client import (
    BACKEND_REQUEST_TIMEOUT_SECONDS,
    COMPENSATION_MAX_WORKERS,
    ENV_ACCESS_TOKEN,
    ENV_ORGANIZATION_ID,
    ENV_TENANT_ID,
    ENV_TRACE_ID,
    GOVERN_API_PATH,
    TENANT_HEADER,
    build_governance_url,
    governance_request_headers,
    resolve_job_context,
    resolve_organization_id,
    resolve_tenant_id,
)

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# Bounded thread pool — caps both concurrent threads AND queued work.
#
# ThreadPoolExecutor alone caps concurrent worker threads, but its internal
# queue is unbounded — a misbehaving agent that fires compensation faster than
# the server can absorb would queue indefinitely (memory pressure). The
# semaphore caps total in-flight submissions (running + queued) at a
# multiple of the worker count. Saturated submissions are dropped with a
# warning. Process exit cancels queued work and lets running tasks finish
# (bounded by their HTTP timeout) via the atexit handler.
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
    running. Tasks already running finish bounded by their HTTP
    timeout (``BACKEND_REQUEST_TIMEOUT_SECONDS``).
    """
    try:
        _pool.shutdown(wait=False, cancel_futures=True)
    except Exception:  # noqa: BLE001 - shutdown must never raise from atexit
        pass


# ----------------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------------


class FiredRule(TypedDict):
    """Per-rule metadata carried in the /runtime/govern payload.

    One entry per matching ``guardrail_fallback`` condition (in practice
    one per rule, since each fallback-rule typically declares a single
    such condition). The server uses these to write per-rule LLMOps
    trace records (Doc-2 audit structure).
    """

    ruleId: str
    ruleName: str
    packName: str
    validator: str


def disabled_guardrails(audit: Any, policy_index: Any) -> list[FiredRule]:
    """Return per-rule metadata for each fired guardrail-fallback rule.

    A guardrail rule fires only when it is mapped to UiPath
    (``mapped_to_uipath`` true) but disabled (``policy_enabled`` false) —
    see the ``guardrail_fallback`` operator. The validator name (e.g.
    ``pii_detection``) is read from the rule's ``guardrail_fallback``
    check config and used as the ``type`` of the compensating call.

    One :class:`FiredRule` entry is emitted per matching
    ``guardrail_fallback`` condition. Rules in this codebase declare a
    single fallback condition each, so the returned list has one entry
    per fired rule in practice; multi-condition rules would emit more
    than one entry sharing the same ``ruleId``.

    Each entry carries the metadata the server needs to write one
    per-rule LLMOps trace record::

        {
          "ruleId": "...",
          "ruleName": "...",
          "packName": "...",
          "validator": "pii_detection",
        }
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
                        {
                            "ruleId": ev.rule_id,
                            "ruleName": ev.rule_name,
                            "packName": getattr(rule, "pack_name", "") or "",
                            "validator": validator,
                        }
                    )
    return out


def _validators(rules: list[FiredRule]) -> list[str]:
    """Distinct validator names from the fired rules, preserving order."""
    return list(dict.fromkeys(r["validator"] for r in rules if r.get("validator")))


def _resolve_trace_id(fallback: str) -> str:
    """Resolve the agent's trace id while still on the caller thread.

    MUST be called before the background-pool hop in
    :func:`submit_compensation`: the worker thread that issues the
    ``/govern`` call has no OpenTelemetry context, so resolving there would
    miss the live span and fall back to a detached id — orphaning the
    server-written compensation records from the agent's real trace (which
    is exactly what the native audit spans bind to).

    Order: live OTel span trace id (32-char hex) -> ``UIPATH_TRACE_ID``
    env var -> the caller-supplied ``fallback``.
    """
    try:
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        if ctx.is_valid:
            return format(ctx.trace_id, "032x")
    except Exception:  # noqa: BLE001 - tracing is best-effort; fall through
        pass

    env_trace_id = os.environ.get(ENV_TRACE_ID)
    if env_trace_id:
        return env_trace_id

    return fallback


def submit_compensation(
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
    # agent's OTel span is still live. The /govern call below runs on a
    # background worker (_pool.submit -> _run -> request_governance) where
    # that context is gone, so the resolved value is captured now and
    # carried into the worker — ensuring the server writes compensation
    # records under the agent's real trace, not a detached id.
    trace_id = _resolve_trace_id(trace_id)

    if not _inflight.acquire(blocking=False):
        logger.warning(
            "Compensation pool saturated (>%d in flight); dropping call "
            "(validators=[%s])",
            _INFLIGHT_CAP,
            ", ".join(validators),
        )
        return

    def _run() -> None:
        try:
            request_governance(
                rules=rules,
                data=data,
                hook=hook,
                trace_id=trace_id,
                src_timestamp=src_timestamp,
                agent_name=agent_name,
                runtime_id=runtime_id,
            )
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


def request_governance(
    rules: list[FiredRule],
    data: dict[str, Any],
    hook: str,
    trace_id: str,
    src_timestamp: str,
    agent_name: str,
    runtime_id: str,
) -> None:
    """Synchronous POST to the org-scoped ``/runtime/govern`` endpoint.

    Most callers should use :func:`submit_compensation` to run this on
    the bounded background pool. ``request_governance`` is exposed
    directly only for callers that already manage their own
    concurrency (and for tests).

    POSTs::

        {
          "type": ["pii_detection", "harmful_content"],
          "rules": [
            {"ruleId": "...", "ruleName": "...",
             "packName": "...", "validator": "pii_detection"}
          ],
          "data": {...},
          "hook": "before_model",
          "traceId": "...",
          "src_timestamp": "...",
          "agentName": "...",
          "runtimeId": "...",
          "folderKey": "...", "jobKey": "...", "processKey": "...",
          "referenceId": "...", "agentVersion": "..."
        }

    ``type`` (the distinct validators) drives the guardrail API call;
    ``rules`` + the job-context fields let the server write one LLMOps
    trace record per rule (Doc-2 audit structure). The job-context keys
    are included only when resolvable from the environment.

    Skipped if the org or tenant id can't be resolved (no URL / no
    header). The server runs the disabled guardrails AND writes the
    audit trace itself — the agent does not consume or parse the
    response body. The only thing this function reports back is
    *whether the call landed*:

    - **Success** → ``INFO`` log ``Govern call has been made``.
    - **Failure** → ``WARNING`` log; returns ``None``.

    Never raises.
    """
    if not rules:
        return

    validators = _validators(rules)
    if not validators:
        return

    org_id = resolve_organization_id()
    if not org_id:
        logger.warning(
            "Govern call skipped: organization id is not available "
            "(set %s). validators=[%s]",
            ENV_ORGANIZATION_ID,
            ", ".join(validators),
        )
        return

    tenant_id = resolve_tenant_id()
    if not tenant_id:
        logger.warning(
            "Govern call skipped: tenant id is not available "
            "(set %s). validators=[%s]",
            ENV_TENANT_ID,
            ", ".join(validators),
        )
        return

    # Bearer token is required by the backend; sending without one
    # produces a 401 per call and pollutes logs. Skip cleanly when the
    # token isn't present (e.g. local dev, missing host bootstrap)
    # rather than burning quota on guaranteed auth failures.
    if not os.environ.get(ENV_ACCESS_TOKEN):
        logger.warning(
            "Govern call skipped: %s is not set in the environment; "
            "compensation requires a bearer token. validators=[%s]",
            ENV_ACCESS_TOKEN,
            ", ".join(validators),
        )
        return

    try:
        payload = json.dumps(
            {
                "type": validators,
                "rules": rules,
                "data": data,
                "hook": hook,
                "traceId": trace_id,
                "src_timestamp": src_timestamp,
                "agentName": agent_name,
                "runtimeId": runtime_id,
                **resolve_job_context(),
            },
            default=str,  # coerce any non-JSON-native value safely
        ).encode("utf-8")
    except Exception as exc:  # noqa: BLE001 - fail-open
        logger.warning(
            "Govern call payload serialization failed (validators=[%s]): %s",
            ", ".join(validators),
            exc,
        )
        return

    url = build_governance_url(org_id, GOVERN_API_PATH)
    headers = governance_request_headers(json_body=True)
    headers[TENANT_HEADER] = tenant_id

    request = urllib.request.Request(
        url,
        data=payload,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(  # noqa: S310 - URL is built from config
            request, timeout=BACKEND_REQUEST_TIMEOUT_SECONDS
        ) as response:
            logger.info(
                "Govern call has been made (status=%s, validators=[%s])",
                getattr(response, "status", "?"),
                ", ".join(validators),
            )
    except Exception as exc:  # noqa: BLE001 - fail-and-log
        logger.warning(
            "Govern call failed (validators=[%s]): %s",
            ", ".join(validators),
            exc,
        )
