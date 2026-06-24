"""Governance runtime wrapper.

Wraps a :class:`UiPathRuntimeProtocol` delegate so policy data is sourced
through a :class:`GovernancePolicyProvider`. The provider owns the wire
/ transport (auth, retries, telemetry); the runtime only consumes the
parsed :class:`PolicyResponse`. There is no direct backend fallback —
when ``policy_provider`` is ``None`` the agent runs without any
governance policies.

The wiring layer (uipath CLI) decides whether to construct
``GovernanceRuntime`` at all (feature flag, project config, etc.) and
passes ``is_conversational`` and ``trace_id`` explicitly. The runtime
layer does not introspect the delegate's private attributes nor read
env vars to discover those.

**Staging caveat — policy loading only, no enforcement yet.** This
module is the policy-loading scaffold: ``__init__`` constructs an
instance-scoped :class:`PolicyLoader` and kicks off a background
prefetch. ``execute`` / ``stream`` / ``get_schema`` / ``dispose`` are
pure passthroughs — no per-hook policy evaluation runs. The evaluator
and framework adapter wiring that consumes the loader's policy index
and the ``trace_id`` lands in a follow-up slice. Customers constructing
:class:`GovernanceRuntime` today get policy loading without policy
enforcement; this is intentional and will change when the evaluator
slice merges.
"""

from __future__ import annotations

import logging
from typing import Any, AsyncGenerator

from uipath.core.governance import GovernancePolicyProvider

from uipath.runtime.base import (
    UiPathExecuteOptions,
    UiPathRuntimeProtocol,
    UiPathStreamOptions,
)
from uipath.runtime.events import UiPathRuntimeEvent
from uipath.runtime.governance.native.loader import PolicyLoader
from uipath.runtime.result import UiPathRuntimeResult
from uipath.runtime.schema import UiPathRuntimeSchema

logger = logging.getLogger(__name__)


class GovernanceRuntime:
    """Governance wrapper over a :class:`UiPathRuntimeProtocol` delegate.

    Constructs an instance-scoped :class:`PolicyLoader` bound to the
    supplied provider and kicks off a non-blocking prefetch so the
    policy pack overlaps with the rest of agent setup. When
    ``policy_provider`` is ``None``, the loader yields an empty
    PolicyIndex and the agent runs without any governance policies for
    the lifetime of this instance.

    **Policy loading only — no enforcement yet.** ``execute`` / ``stream``
    / ``get_schema`` / ``dispose`` are passthroughs to the delegate; no
    per-hook policy evaluation runs in this slice. The evaluator and
    framework adapter wiring that consumes the loader's policy index is
    staged separately.
    """

    def __init__(
        self,
        delegate: UiPathRuntimeProtocol,
        policy_provider: GovernancePolicyProvider | None,
        *,
        is_conversational: bool | None = None,
        trace_id: str | None = None,
    ):
        """Initialize the governance runtime.

        Args:
            delegate: The wrapped runtime to forward execution to.
            policy_provider: Source of the policy pack. ``None`` means
                no policies will be loaded — the agent runs without
                governance for the lifetime of this instance.
            is_conversational: Whether the hosted agent is
                conversational. Forwarded into the provider's
                :class:`PolicyContext` so it can pick the right policy
                view (conversational vs autonomous). ``None`` (default)
                leaves the selector unset — the provider applies its
                default. The wiring layer (uipath CLI) is expected to
                pass the concrete value when it knows the agent type.
            trace_id: Trace identifier the platform host has bound to
                this run (typically read from ``UIPATH_TRACE_ID`` by
                the wiring layer). The evaluator slice forwards this
                into the :class:`GuardrailCompensator` so server-written
                compensation records land on the agent's run trace
                instead of a detached id. ``None`` (default) leaves
                downstream consumers to fall back to the live OTel
                span / caller-supplied value.
        """
        self._delegate = delegate
        self._trace_id = trace_id
        self._loader = PolicyLoader(
            policy_provider,
            is_conversational=is_conversational,
        )
        self._loader.prefetch()

    @property
    def loader(self) -> PolicyLoader:
        """The instance-scoped policy loader.

        Exposed so adapters / evaluators wired into this runtime can
        call :meth:`PolicyLoader.get_policy_index` at hook time.
        """
        return self._loader

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
