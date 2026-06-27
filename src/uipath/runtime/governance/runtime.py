"""Governance runtime wrapper.

Wraps a :class:`UiPathRuntimeProtocol` delegate and carries a resolved
policy snapshot — a :class:`PolicyIndex` and :class:`EnforcementMode`
supplied by the caller. The wrapper performs no I/O at construction,
holds no background thread, retains no policy provider, and reads no
host environment variables.

The caller (typically the host CLI) is expected to:

- ``await provider.get_policy_async(PolicyContext(...))`` itself,
- compile the response YAML via
  :func:`uipath.runtime.governance.native.build_policy_index_from_yaml`,
- skip wrapping entirely when the response mode is
  :attr:`EnforcementMode.DISABLED`,
- pass the resolved ``PolicyIndex`` and ``EnforcementMode`` into the
  constructor.

The wrapper owns the BEFORE_AGENT / AFTER_AGENT lifecycle boundary
when an evaluator is supplied at construction. Framework adapters
intentionally skip chain-level events so nested chain runs don't fire
duplicate boundary evaluations; the runtime layer is the unambiguous
"one invocation = one boundary" point, so it owns those hooks. Per-step
hooks (BEFORE_MODEL, AFTER_MODEL, TOOL_CALL, AFTER_TOOL) are fired by
adapters that observe per-step events.

Trace correlation: :meth:`UiPathGovernedRuntime.execute` and
:meth:`UiPathGovernedRuntime.stream` open a single OTel span that
wraps BEFORE_AGENT, the delegate's run (including any framework
adapter spans), and AFTER_AGENT. The span inherits the host's
ambient context when one is set, or opens a new root trace
otherwise. Every governance event emitted under that span shares
its ``trace_id``, so runtime-layer telemetry and framework-layer
OTel spans correlate end-to-end. Downstream consumers map that
``trace_id`` to whatever correlation field they use.

The governance compensator dispatches synchronously on the caller's
thread; the injected compensation provider resolves the canonical
trace id at call time. The runtime layer remains env-free for that
path.
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from typing import Any, AsyncGenerator, Callable, Iterator

from uipath.core.governance import EnforcementMode
from uipath.core.governance.exceptions import GovernanceBlockException
from uipath.core.serialization import serialize_object

from uipath.runtime.base import (
    UiPathExecuteOptions,
    UiPathRuntimeProtocol,
    UiPathStreamOptions,
)
from uipath.runtime.events import UiPathRuntimeEvent
from uipath.runtime.governance.native.evaluator import GovernanceEvaluator
from uipath.runtime.governance.native.models import PolicyIndex
from uipath.runtime.result import UiPathRuntimeResult
from uipath.runtime.schema import UiPathRuntimeSchema

logger = logging.getLogger(__name__)


@contextmanager
def _governance_root_span(agent_name: str, runtime_id: str) -> Iterator[None]:
    """Open the governance run's root OTel span if OTel is available.

    Every governance event and every framework OTel span emitted
    inside this block inherits the span's ``trace_id``. The result
    is one trace per agent run that correlates BEFORE_AGENT,
    BEFORE_MODEL / AFTER_MODEL, AFTER_AGENT, and any per-rule
    governance events.

    Behavior matrix:

    - **OTel installed + host opened a parent span**: this becomes a
      child of the host's span and inherits its ``trace_id`` — the
      host's outer correlation context is preserved end-to-end.
    - **OTel installed + no parent span**: this becomes the root
      span of a fresh trace; everything below it shares the new
      ``trace_id``.
    - **OTel not installed**: no-op context manager — the wrapper
      degrades gracefully (no spans, no correlation, but the
      runtime still functions).

    Lazy import mirrors
    :func:`uipath.runtime.governance._audit.track_events._resolve_operation_id`:
    OTel is a transitive runtime dependency, but the wrapper must
    not crash if a downstream host strips it.
    """
    try:
        from opentelemetry import trace
    except ImportError:
        yield
        return

    tracer = trace.get_tracer("uipath.runtime.governance")
    # No explicit ``context=`` → OTel picks up the ambient context.
    # If the host wrapped this call in its own span, we become its
    # child (same trace_id). Otherwise we open a root span (new
    # trace_id).
    with tracer.start_as_current_span("uipath.governance.run") as span:
        # Span attributes for downstream consumers. ``agent_name``
        # and ``runtime_id`` are the primary keys an operator
        # filters on; ``source`` identifies which producer emitted
        # the span (mirrors the constant used by
        # :class:`TracesAuditSink` so consumers stay consistent).
        if agent_name:
            span.set_attribute("uipath_governance.agent_name", agent_name)
        if runtime_id:
            span.set_attribute("uipath_governance.runtime_id", runtime_id)
        span.set_attribute(
            "uipath_governance.source", "governance-runtime-python"
        )
        yield


def _serialize_payload(payload: Any) -> str:
    """Serialize an agent input / output to a string for evaluator checks.

    The native evaluator's BEFORE_AGENT / AFTER_AGENT checks scan a
    flat string. ``None`` becomes ``""``, ``str`` passes through (so
    regex / sentiment checks don't see JSON quotes around the bare
    text), and everything else is normalized via
    :func:`uipath.core.serialization.serialize_object` (handles
    Pydantic / dataclass / datetime / nested structures) and then
    JSON-encoded.
    """
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    try:
        return json.dumps(serialize_object(payload))
    except Exception:  # noqa: BLE001 — last-resort string fallback
        return str(payload)


class UiPathGovernedRuntime:
    """Governance wrapper over a :class:`UiPathRuntimeProtocol` delegate.

    Holds a caller-resolved :class:`PolicyIndex` and
    :class:`EnforcementMode` for the lifetime of the instance.
    ``execute`` / ``stream`` / ``get_schema`` / ``dispose`` forward to
    the delegate.

    When ``evaluator`` is supplied, :meth:`execute` and :meth:`stream`
    fire ``BEFORE_AGENT`` before delegating and ``AFTER_AGENT`` after a
    successful return. Without an evaluator the wrapper is a pure
    pass-through.
    """

    def __init__(
        self,
        delegate: UiPathRuntimeProtocol,
        policy_index: PolicyIndex,
        enforcement_mode: EnforcementMode,
        *,
        evaluator: GovernanceEvaluator | None = None,
        agent_name: str = "",
        runtime_id: str = "",
        on_dispose: Callable[[], None] | None = None,
    ):
        """Initialize the governance runtime with a resolved policy snapshot.

        Args:
            delegate: The wrapped runtime to forward execution to.
            policy_index: Resolved :class:`PolicyIndex` built from the
                provider's :class:`PolicyResponse`. Pass an empty
                ``PolicyIndex()`` to attach the wrapper without any
                rules (useful when the wrapper exists for audit
                emission only).
            enforcement_mode: Resolved :class:`EnforcementMode` from
                the provider's :class:`PolicyResponse`. The caller is
                expected to skip wrapping entirely when the response
                mode is :attr:`EnforcementMode.DISABLED`; this
                constructor does not check.
            evaluator: Optional :class:`GovernanceEvaluator` that
                drives BEFORE_AGENT / AFTER_AGENT inside
                :meth:`execute` / :meth:`stream`. When ``None`` the
                wrapper is a pure passthrough — the caller is expected
                to fire those evaluations itself.
            agent_name: Name of the agent (the runtime's entrypoint).
                Passed through to the evaluator's hook methods.
            runtime_id: Runtime-instance id (conversation id, job id,
                or a synthetic per-run id). Passed through so
                per-runtime state routes cleanly.
            on_dispose: Optional host-supplied cleanup callback invoked
                from :meth:`dispose` after the delegate has been
                disposed. Called in a ``finally`` so it runs even when
                the delegate raises. Lets the host attach any
                per-runtime teardown (flushing a telemetry dispatcher,
                closing a session, etc.) to the same lifecycle path
                the runtime already owns — so callers on every CLI
                path don't have to remember to run it themselves. The
                runtime treats it as an opaque ``Callable[[], None]``
                and does not touch the host's underlying object.
        """
        self._delegate = delegate
        self._policy_index = policy_index
        self._enforcement_mode = enforcement_mode
        self._evaluator = evaluator
        self._agent_name = agent_name
        self._runtime_id = runtime_id
        self._on_dispose = on_dispose

    def _fire_before_agent(self, input: Any) -> None:
        """Fire BEFORE_AGENT when an evaluator is wired; otherwise no-op.

        ``GovernanceBlockException`` propagates — that's how
        ENFORCE-mode DENY rules halt a run. Anything else is logged
        and swallowed so a governance bug never breaks the agent.
        """
        if self._evaluator is None:
            return
        try:
            self._evaluator.evaluate_before_agent(
                agent_input=_serialize_payload(input),
                agent_name=self._agent_name,
                runtime_id=self._runtime_id,
            )
        except GovernanceBlockException:
            raise
        except Exception as exc:  # noqa: BLE001 — never break a run on audit failure
            logger.warning("BEFORE_AGENT governance evaluation failed: %s", exc)

    def _fire_after_agent(self, result: UiPathRuntimeResult) -> None:
        """Fire AFTER_AGENT against ``result.output``.

        Same exception policy as :meth:`_fire_before_agent`.
        """
        if self._evaluator is None:
            return
        try:
            self._evaluator.evaluate_after_agent(
                agent_output=_serialize_payload(result.output),
                agent_name=self._agent_name,
                runtime_id=self._runtime_id,
            )
        except GovernanceBlockException:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("AFTER_AGENT governance evaluation failed: %s", exc)

    async def execute(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathExecuteOptions | None = None,
    ) -> UiPathRuntimeResult:
        """Execute the delegate, firing BEFORE_AGENT / AFTER_AGENT around it.

        Wraps the entire invocation — BEFORE_AGENT, delegate execution
        (which may itself open framework spans), and AFTER_AGENT — in
        a single OTel span. The shared ``trace_id`` unifies the
        runtime-side governance events with any framework-side OTel
        spans the delegate emits.

        AFTER_AGENT fires only on successful return — if the delegate
        raises, there's no output to evaluate.
        """
        with _governance_root_span(self._agent_name, self._runtime_id):
            self._fire_before_agent(input)
            result = await self._delegate.execute(input, options=options)
            self._fire_after_agent(result)
            return result

    async def stream(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathStreamOptions | None = None,
    ) -> AsyncGenerator[UiPathRuntimeEvent, None]:
        """Stream events from the delegate, firing BEFORE_AGENT first.

        Same root-span wrap as :meth:`execute`. Holding the span
        across ``async for`` is safe — Python asyncio propagates
        contextvars (including the OTel current-span pointer) across
        ``await`` suspensions.

        AFTER_AGENT fires once a :class:`UiPathRuntimeResult` event is
        observed in the stream — that's the runtime's contract for
        signalling a completed invocation. Intermediate state events
        pass through untouched.
        """
        with _governance_root_span(self._agent_name, self._runtime_id):
            self._fire_before_agent(input)
            async for event in self._delegate.stream(input, options=options):
                if isinstance(event, UiPathRuntimeResult):
                    self._fire_after_agent(event)
                yield event

    async def get_schema(self) -> UiPathRuntimeSchema:
        """Forward schema lookup to the delegate."""
        return await self._delegate.get_schema()

    async def dispose(self) -> None:
        """Dispose the delegate, then run the caller's cleanup hook.

        Runs ``on_dispose`` (if supplied at construction) in a
        ``finally`` so host-owned teardown — e.g. flushing a telemetry
        dispatcher — happens even when the delegate raises. The runtime
        does not inspect what the hook does; it's an opaque callable.
        """
        try:
            await self._delegate.dispose()
        finally:
            if self._on_dispose is not None:
                self._on_dispose()
