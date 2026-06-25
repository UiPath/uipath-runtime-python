"""Governance runtime wrapper.

Wraps a :class:`UiPathRuntimeProtocol` delegate. The wrapper is
**pure** — it holds an already-resolved :class:`PolicyIndex` and
:class:`EnforcementMode` passed in by the host. No I/O happens at
construction, no background thread is spun up, no provider is held.

Why: per the architecture-review §2.4 prescription, the policy fetch
belongs to the async host (uipath CLI), which does
``await provider.get_policy_async(PolicyContext(is_conversational=...))``
itself, compiles the response YAML via
:func:`build_policy_index_from_yaml`, and hands the resolved
``PolicyIndex`` + mode into this constructor. The runtime layer
becomes a passive consumer of a snapshot; the host owns lifecycle
(refetch, refresh, dispose).

Agent-type selection (``is_conversational``) lives in the host's
:class:`PolicyContext` construction, not on this wrapper. The
generic runtime layer no longer carries that selector.

**Staging caveat — policy data only, no enforcement yet.** ``execute``
/ ``stream`` / ``get_schema`` / ``dispose`` are pure passthroughs;
per-hook policy evaluation lands in a follow-up slice that wires the
evaluator into the host's decorator chain. Constructing
:class:`GovernanceRuntime` today gives you the resolved policy
snapshot exposed via :attr:`policy_index` and :attr:`enforcement_mode`
for the evaluator to pick up.
"""

from __future__ import annotations

import logging
from typing import Any, AsyncGenerator

from uipath.core.governance import EnforcementMode

from uipath.runtime.base import (
    UiPathExecuteOptions,
    UiPathRuntimeProtocol,
    UiPathStreamOptions,
)
from uipath.runtime.events import UiPathRuntimeEvent
from uipath.runtime.governance.native.models import PolicyIndex
from uipath.runtime.result import UiPathRuntimeResult
from uipath.runtime.schema import UiPathRuntimeSchema

logger = logging.getLogger(__name__)


class GovernanceRuntime:
    """Governance wrapper over a :class:`UiPathRuntimeProtocol` delegate.

    The constructor takes a **resolved** :class:`PolicyIndex` and
    :class:`EnforcementMode` — the host has already done the async
    fetch via the policy provider and compiled the YAML. The runtime
    holds the snapshot for the lifetime of the wrapping instance.

    **Policy data only — no enforcement yet.** ``execute`` / ``stream``
    / ``get_schema`` / ``dispose`` are passthroughs to the delegate;
    the evaluator + framework adapter that consume
    :attr:`policy_index` / :attr:`enforcement_mode` are staged
    separately.
    """

    def __init__(
        self,
        delegate: UiPathRuntimeProtocol,
        policy_index: PolicyIndex,
        enforcement_mode: EnforcementMode,
        *,
        trace_id: str | None = None,
    ):
        """Initialize the governance runtime with a resolved policy snapshot.

        Args:
            delegate: The wrapped runtime to forward execution to.
            policy_index: Resolved :class:`PolicyIndex` the host built
                from the provider's :class:`PolicyResponse`. Pass an
                empty ``PolicyIndex()`` to attach the wrapper without
                any rules (useful when the wrapper exists for audit
                emission only).
            enforcement_mode: Resolved :class:`EnforcementMode` from
                the provider's :class:`PolicyResponse`. The host is
                expected to skip wrapping entirely when the response
                mode is :attr:`EnforcementMode.DISABLED`; this
                constructor doesn't check.
            trace_id: Trace identifier the platform host bound to this
                run (typically read from ``UIPATH_TRACE_ID`` by the
                wiring layer). Forwarded to the
                :class:`GuardrailCompensator` by the evaluator slice
                so server-written compensation records land on the
                agent's run trace. ``None`` (default) leaves
                downstream consumers to fall back to the live OTel
                span / caller-supplied value.
        """
        self._delegate = delegate
        self._policy_index = policy_index
        self._enforcement_mode = enforcement_mode
        self._trace_id = trace_id

    @property
    def policy_index(self) -> PolicyIndex:
        """The resolved policy snapshot this runtime evaluates against.

        Exposed so the evaluator slice can pick it up when it wires
        per-hook evaluation into ``execute`` / ``stream``.
        """
        return self._policy_index

    @property
    def enforcement_mode(self) -> EnforcementMode:
        """The enforcement mode the host supplied at construction."""
        return self._enforcement_mode

    @property
    def trace_id(self) -> str | None:
        """Trace id supplied by the wiring layer (or ``None``).

        Exposed so the evaluator slice can read it at hook-wire time
        and pass it into the :class:`GuardrailCompensator` it
        constructs.
        """
        return self._trace_id

    async def execute(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathExecuteOptions | None = None,
    ) -> UiPathRuntimeResult:
        """Execute the delegate. Policy evaluation hooks are wired separately."""
        return await self._delegate.execute(input, options=options)

    async def stream(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathStreamOptions | None = None,
    ) -> AsyncGenerator[UiPathRuntimeEvent, None]:
        """Stream events from the delegate. Hooks are wired separately."""
        async for event in self._delegate.stream(input, options=options):
            yield event

    async def get_schema(self) -> UiPathRuntimeSchema:
        """Passthrough schema for the delegate."""
        return await self._delegate.get_schema()

    async def dispose(self) -> None:
        """Dispose the delegate."""
        await self._delegate.dispose()
