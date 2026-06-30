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

Trace-id is intentionally **not** carried on this wrapper. The
governance compensator dispatches synchronously on the caller's
thread; the injected provider resolves the canonical trace id at
call time. The runtime layer is fully env-free for this path.
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncGenerator

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
        """
        self._delegate = delegate
        self._policy_index = policy_index
        self._enforcement_mode = enforcement_mode
        self._evaluator = evaluator
        self._agent_name = agent_name
        self._runtime_id = runtime_id

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

        AFTER_AGENT fires only on successful return — if the delegate
        raises, there's no output to evaluate.
        """
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

        AFTER_AGENT fires once a :class:`UiPathRuntimeResult` event is
        observed in the stream — that's the runtime's contract for
        signalling a completed invocation. Intermediate state events
        pass through untouched.
        """
        self._fire_before_agent(input)
        async for event in self._delegate.stream(input, options=options):
            if isinstance(event, UiPathRuntimeResult):
                self._fire_after_agent(event)
            yield event

    async def get_schema(self) -> UiPathRuntimeSchema:
        """Forward schema lookup to the delegate."""
        return await self._delegate.get_schema()

    async def dispose(self) -> None:
        """Forward disposal to the delegate."""
        await self._delegate.dispose()
