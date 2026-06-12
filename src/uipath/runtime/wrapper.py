"""Governance integration for the runtime.

Wraps a runtime with governance when the ``EnablePythonGovernanceChecker``
feature flag is enabled. ``uipath.runtime.governance.wrapper`` is
imported lazily — only when the gate passes — so its transitive cost
(audit, evaluator, OTel, …) stays off the startup path when governance
is disabled.

The feature flag name and gate function are re-exported from
``uipath.core.governance.config`` so there is a single source of truth.
"""

from __future__ import annotations

import logging

from uipath.core.governance.config import (
    GOVERNANCE_FEATURE_FLAG,
    is_governance_enabled,
)

from uipath.runtime.base import UiPathRuntimeProtocol
from uipath.runtime.context import UiPathRuntimeContext

logger = logging.getLogger(__name__)

__all__ = ["GOVERNANCE_FEATURE_FLAG", "apply_governance_wrapper"]


async def apply_governance_wrapper(
    runtime: "UiPathRuntimeProtocol",
    context: "UiPathRuntimeContext | None",
    runtime_id: str,
) -> "UiPathRuntimeProtocol":
    """Wrap a runtime with governance when the feature flag is enabled.

    Returns the inner runtime unchanged when the flag is off or when
    the wrapper itself raises — governance failures must never break
    the agent run.
    """
    if not is_governance_enabled():
        logger.debug(
            "Skipping governance wrapper: %s feature flag is not enabled",
            GOVERNANCE_FEATURE_FLAG,
        )
        return runtime

    from uipath.runtime.governance.wrapper import governance_wrapper

    try:
        return governance_wrapper(runtime, context, runtime_id)
    except Exception as exc:
        logger.warning("Failed to apply governance wrapper: %s", exc)
        return runtime
