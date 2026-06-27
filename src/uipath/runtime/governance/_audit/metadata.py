"""Per-runtime metadata stamped on every governance telemetry event.

The host constructs a :class:`GovernanceRuntimeMetadata` once per
agent run and passes it to the telemetry sink. Every event produced
by :class:`TrackEventAuditSink` carries these fields so downstream
consumers can pivot on engine / agent type / framework / runtime
version.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version

NATIVE_EXECUTION_ENGINE = "uipath_native_governance_checker"


def _resolve_runtime_version() -> str:
    """Read the ``uipath-runtime`` package version, or ``"unknown"``.

    ``importlib.metadata.version`` fails when the package is imported
    from a source checkout that was never installed (CI fixtures,
    editable installs with stripped metadata). Telemetry must keep
    flowing in those cases, so the fallback is a sentinel rather than
    a raise.
    """
    try:
        return version("uipath-runtime")
    except PackageNotFoundError:
        return "unknown"


@dataclass(frozen=True)
class GovernanceRuntimeMetadata:
    """Constants stamped on every governance telemetry event.

    Attributes:
        execution_engine: Implementation behind the evaluator. Default
            ``"uipath_native_governance_checker"``. When a future engine
            (e.g. AGT) replaces the native checker, the host supplies
            its own identifier here so the emitted event records which
            engine produced the verdict.
        agent_type: Category of agent under governance — e.g.
            ``"uipath_coded"``, ``"uipath_lowcode"``, ``"servicenow"``,
            or any other identifier the host wants to attach. External
            agents (ServiceNow, etc.) join this taxonomy when they
            land. ``"unknown"`` keeps telemetry flowing if the host
            forgets to set it.
        agent_framework: Framework that drives the agent — e.g.
            ``"langchain"``, ``"openai_agents"``, ``"llamaindex"``,
            ``"google_adk"``, ``"agent_framework"``, ``"mcp"``.
        runtime_version: ``uipath-runtime`` package version. Resolved
            from installed package metadata at construction; falls
            back to ``"unknown"`` for source checkouts.
    """

    execution_engine: str = NATIVE_EXECUTION_ENGINE
    agent_type: str = "unknown"
    agent_framework: str = "unknown"
    runtime_version: str = field(default_factory=_resolve_runtime_version)

    def as_payload(self) -> dict[str, str]:
        """Return the metadata as a dict ready to merge into an event payload."""
        return {
            "execution_engine": self.execution_engine,
            "agent_type": self.agent_type,
            "agent_framework": self.agent_framework,
            "runtime_version": self.runtime_version,
        }
