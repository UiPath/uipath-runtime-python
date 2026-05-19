"""Governance integration for the runtime.

Wraps a runtime with governance when the ``EnablePythonGovernanceChecker``
feature flag is enabled. ``uipath-governance`` is imported lazily — only
when the gate passes — so its transitive cost (audit, evaluator, OTel,
…) stays off the startup path when governance is disabled.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from uipath.core.feature_flags import FeatureFlags

if TYPE_CHECKING:
    from uipath.runtime.base import UiPathRuntimeProtocol
    from uipath.runtime.context import UiPathRuntimeContext

logger = logging.getLogger(__name__)

GOVERNANCE_FEATURE_FLAG = "EnablePythonGovernanceChecker"


def _is_governance_enabled() -> bool:
    """Return True iff the governance wrapper should be applied.

    Strict opt-in: ``default=False`` mirrors the governance-side gate.
    Governance is applied only when the flag is explicitly truthy in the
    in-process FeatureFlags registry or the ``UIPATH_FEATURE_*`` env-var
    fallback.
    """
    return FeatureFlags.is_flag_enabled(GOVERNANCE_FEATURE_FLAG, default=False)


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
    if not _is_governance_enabled():
        logger.debug(
            "Skipping governance wrapper: %s feature flag is not enabled",
            GOVERNANCE_FEATURE_FLAG,
        )
        return runtime

    from uipath.governance.wrapper import governance_wrapper

    try:
        return governance_wrapper(runtime, context, runtime_id)
    except Exception as exc:
        logger.warning("Failed to apply governance wrapper: %s", exc)
        return runtime
