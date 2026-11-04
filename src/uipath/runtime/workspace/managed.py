"""Shared wiring that persists a runtime's workspace through job attachments."""

from typing import Callable, Protocol

from uipath.core.workspace import AttachmentsProtocol, JobsProtocol

from uipath.runtime.base import UiPathRuntimeProtocol
from uipath.runtime.workspace.conversational import ConversationalWorkspaceRuntime
from uipath.runtime.workspace.hydration import HydrationPolicy, HydrationRuntime
from uipath.runtime.workspace.hydrator import WorkspaceHydrator
from uipath.runtime.workspace.registry_store import WorkspaceRegistryStore
from uipath.runtime.workspace.workspace import Workspace


class WorkspaceServicesProtocol(Protocol):
    """Platform services the workspace hydrator talks to, such as ``UiPath()``."""

    @property
    def attachments(self) -> AttachmentsProtocol:
        """Uploads and downloads workspace files."""
        ...

    @property
    def jobs(self) -> JobsProtocol:
        """Links uploaded files to the current job."""
        ...


class ManagedWorkspace:
    """Builds the workspace wrappers for one runtime.

    The wrappers returned by :meth:`wrap` and :meth:`wrap_conversational` share
    one workspace, one hydrator and one registry store. The platform services
    are created on first use: :meth:`wrap` defers them to the first run, while
    :meth:`wrap_conversational` needs them when it is called.
    """

    def __init__(
        self,
        *,
        services: Callable[[], WorkspaceServicesProtocol],
        runtime_id: str,
        job_key: str | None,
        folder_key: str | None = None,
        workspace: Workspace | None = None,
        registry_store: WorkspaceRegistryStore | None = None,
    ) -> None:
        """Initialize the managed workspace.

        Args:
            services: Returns the attachments and jobs services, for example ``UiPath``.
            runtime_id: Runtime id the wrapped runtime was created with.
            job_key: Job that uploaded files are linked to, or ``None`` outside a job.
            folder_key: Orchestrator folder of the attachments.
            workspace: Folder owned by the caller. When omitted, a temporary
                folder is created; disposing the runtime returned by :meth:`wrap`
                removes it, so call :meth:`wrap` once per instance.
            registry_store: Where the uploaded files are recorded. Required by
                :meth:`wrap`; optional for :meth:`wrap_conversational`.
        """
        self.workspace = workspace if workspace is not None else Workspace.create()
        self.registry_store = registry_store
        self.runtime_id = runtime_id
        self._services = services
        self._job_key = job_key
        self._folder_key = folder_key
        self._hydrator: WorkspaceHydrator | None = None

    def hydrator(self) -> WorkspaceHydrator:
        """Return the shared hydrator, creating the platform services on first use."""
        if self._hydrator is None:
            services = self._services()
            self._hydrator = WorkspaceHydrator(
                workspace_path=self.workspace.path,
                attachments=services.attachments,
                jobs=services.jobs,
                current_job_key=self._job_key,
                folder_key=self._folder_key,
            )
        return self._hydrator

    def wrap(
        self,
        runtime: UiPathRuntimeProtocol,
        *,
        policy: HydrationPolicy = HydrationPolicy.SUSPEND_ONLY,
    ) -> HydrationRuntime:
        """Restore the workspace before each run and persist it according to policy."""
        if self.registry_store is None:
            raise ValueError("Workspace hydration needs a registry store")
        return HydrationRuntime(
            runtime,
            workspace=self.workspace,
            hydrator_factory=self.hydrator,
            registry_store=self.registry_store,
            policy=policy,
            runtime_id=self.runtime_id,
        )

    def wrap_conversational(
        self, runtime: UiPathRuntimeProtocol
    ) -> ConversationalWorkspaceRuntime:
        """Carry the workspace from one conversation exchange to the next.

        The returned runtime never removes the workspace folder. Without a
        caller-owned ``workspace``, also call :meth:`wrap`, whose runtime removes it.
        """
        return ConversationalWorkspaceRuntime(
            runtime,
            hydrator=self.hydrator(),
            registry_store=self.registry_store,
        )
