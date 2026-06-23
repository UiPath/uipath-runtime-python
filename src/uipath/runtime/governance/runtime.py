"""Governance runtime wrapper.

Wraps a :class:`UiPathRuntimeProtocol` delegate so policy data is sourced
through a :class:`GovernancePolicyProvider`. The provider owns the wire
/ transport (auth, retries, telemetry); the runtime only consumes the
parsed :class:`PolicyResponse`. There is no direct backend fallback —
when ``policy_provider`` is ``None`` the agent runs without any
governance policies.

**Staging caveat — policy loading only, no enforcement yet.** This
module is the policy-loading scaffold: ``__init__`` registers the
provider, extracts the conversational/autonomous selector, and kicks
off a background prefetch into the loader cache. ``execute`` /
``stream`` / ``get_schema`` / ``dispose`` are pure passthroughs — no
per-hook policy evaluation runs. The evaluator + adapter wiring that
consumes :func:`get_policy_index` lands in a follow-up slice. Customers
constructing :class:`GovernanceRuntime` today get policy loading without
policy enforcement; this is intentional and will change when the
evaluator slice merges.
"""

from __future__ import annotations

import logging
from typing import Any, AsyncGenerator

from uipath.core.governance import GovernancePolicyProvider
from uipath.core.governance.config import is_governance_enabled

from uipath.runtime.base import (
    UiPathExecuteOptions,
    UiPathRuntimeProtocol,
    UiPathStreamOptions,
)
from uipath.runtime.events import UiPathRuntimeEvent
from uipath.runtime.governance.native.loader import (
    prefetch_policy_index,
    set_agent_conversational,
    set_policy_provider,
)
from uipath.runtime.result import UiPathRuntimeResult
from uipath.runtime.schema import UiPathRuntimeSchema

logger = logging.getLogger(__name__)

# Bound on how deeply we walk ``_delegate`` / ``delegate`` chains when
# looking for an :class:`AgentDefinition`. Wrappers like
# :class:`UiPathExecutionRuntime` and :class:`UiPathResumableRuntime`
# add at most a handful of layers; 10 is well above any realistic
# stack and keeps a pathological self-referential wrapper from looping.
_MAX_DELEGATE_UNWRAP_DEPTH = 10


def _extract_is_conversational(delegate: object) -> bool | None:
    """Read ``is_conversational`` off the delegate's agent definition.

    Walks ``delegate._agent_definition.is_conversational`` (the
    LicensedRuntime pattern published by the agents SDK), unwrapping
    the ``_delegate`` / ``delegate`` chain up to
    :data:`_MAX_DELEGATE_UNWRAP_DEPTH` so wrapper layers don't hide the
    licensed runtime.

    Returns ``None`` when no agent definition is reachable — the
    provider then applies its default rather than the runtime guessing
    a value.
    """
    node: object | None = delegate
    for _ in range(_MAX_DELEGATE_UNWRAP_DEPTH):
        if node is None:
            break
        agent_def = getattr(node, "_agent_definition", None)
        if agent_def is not None:
            value = getattr(agent_def, "is_conversational", None)
            if value is not None:
                return bool(value)
        node = getattr(node, "_delegate", None) or getattr(node, "delegate", None)
    return None


class GovernanceRuntime:
    """Governance wrapper over a :class:`UiPathRuntimeProtocol` delegate.

    Registers the supplied :class:`GovernancePolicyProvider` with the
    policy loader and kicks off a non-blocking prefetch so the policy
    pack overlaps with the rest of agent setup. When ``policy_provider``
    is ``None``, no provider is registered and the agent runs without
    any governance policies (the loader yields an empty PolicyIndex).

    **Policy loading only — no enforcement yet.** ``execute`` / ``stream``
    / ``get_schema`` / ``dispose`` are passthroughs to the delegate; no
    per-hook policy evaluation runs in this slice. The evaluator and
    framework adapter wiring that consumes :func:`get_policy_index` is
    staged separately. Constructing this wrapper today gives you the
    policy load (provider invoked, index cached) but no actual
    enforcement of the loaded rules.
    """

    def __init__(
        self,
        delegate: UiPathRuntimeProtocol,
        policy_provider: GovernancePolicyProvider | None,
    ):
        """Initialize the governance runtime.

        Args:
            delegate: The wrapped runtime to forward execution to.
            policy_provider: Source of the policy pack. ``None`` means
                no policies will be loaded — the agent runs without
                governance for the lifetime of this instance.
        """
        self._delegate = delegate
        self._policy_provider = policy_provider

        if is_governance_enabled():
            # Record agent-type before the prefetch fires so the
            # provider's first ``get_policy`` call sees the right
            # selector on its ``PolicyContext``. Wrapped in try/except
            # so a misbehaving delegate getattr can't break runtime
            # init — fail-open: on failure the selector keeps whatever
            # value an integration may have set externally.
            #
            # Only write when extraction returned a concrete bool. An
            # extraction miss (``None``) leaves the selector untouched
            # so an externally-set value (e.g. an integration that
            # pre-seeded the selector from a different signal) is not
            # silently clobbered by our init.
            try:
                extracted = _extract_is_conversational(delegate)
            except Exception as exc:  # noqa: BLE001 - fail-open
                logger.warning(
                    "Failed to extract is_conversational from delegate: %s", exc
                )
            else:
                if extracted is not None:
                    set_agent_conversational(extracted)
            set_policy_provider(policy_provider)
            prefetch_policy_index()

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
