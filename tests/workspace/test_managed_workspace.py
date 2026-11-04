from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, AsyncGenerator

import pytest

from tests.workspace.test_workspace_hydration import (
    FakeAttachments,
    FakeJobs,
    MemoryStorage,
)
from uipath.runtime import (
    ManagedWorkspace,
    UiPathExecuteOptions,
    UiPathRuntimeResult,
    UiPathRuntimeStatus,
    UiPathStreamOptions,
    Workspace,
    WorkspaceRegistryStore,
    get_workspace_path,
)
from uipath.runtime.events import (
    UiPathRuntimeConversationMetaEvent,
    UiPathRuntimeEvent,
)
from uipath.runtime.schema import UiPathRuntimeSchema

JOB_KEY = str(uuid.uuid4())


class FakeServices:
    def __init__(self) -> None:
        self.attachments = FakeAttachments()
        self.jobs = FakeJobs()
        self.built = 0

    def __call__(self) -> FakeServices:
        self.built += 1
        return self


class ReportRuntime:
    """Writes or reads ``report.md`` in the managed workspace."""

    def __init__(
        self,
        *,
        write: str | None = None,
        status: UiPathRuntimeStatus = UiPathRuntimeStatus.SUCCESSFUL,
    ) -> None:
        self.write = write
        self.status = status
        self.workspace_path: Path | None = None

    async def execute(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathExecuteOptions | None = None,
    ) -> UiPathRuntimeResult:
        self.workspace_path = get_workspace_path()
        report = self.workspace_path / "report.md"
        if self.write is not None:
            report.write_text(self.write)
        content = report.read_text() if report.exists() else None
        return UiPathRuntimeResult(status=self.status, output={"report": content})

    async def stream(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathStreamOptions | None = None,
    ) -> AsyncGenerator[UiPathRuntimeEvent, None]:
        yield await self.execute(input, options)

    async def get_schema(self) -> UiPathRuntimeSchema:
        raise NotImplementedError

    async def dispose(self) -> None:
        pass


def _managed(
    services: FakeServices,
    storage: MemoryStorage,
    workspace: Workspace | None = None,
) -> ManagedWorkspace:
    return ManagedWorkspace(
        services=services,
        runtime_id="runtime-1",
        job_key=JOB_KEY,
        workspace=workspace,
        registry_store=WorkspaceRegistryStore(storage, "runtime-1"),
    )


@pytest.mark.asyncio
async def test_files_written_before_a_suspend_are_back_after_resume() -> None:
    services, storage = FakeServices(), MemoryStorage()

    suspended = _managed(services, storage).wrap(
        ReportRuntime(write="draft", status=UiPathRuntimeStatus.SUSPENDED)
    )
    await suspended.execute()
    await suspended.dispose()
    resumed = _managed(services, storage).wrap(ReportRuntime())
    result = await resumed.execute()
    await resumed.dispose()

    assert result.output == {"report": "draft"}
    assert [job for job, _ in services.jobs.links] == [uuid.UUID(JOB_KEY)]


@pytest.mark.asyncio
async def test_services_are_built_only_when_the_run_starts() -> None:
    services = FakeServices()

    runtime = _managed(services, MemoryStorage()).wrap(ReportRuntime())
    built_before_run = services.built
    await runtime.execute()
    await runtime.execute()

    assert built_before_run == 0
    assert services.built == 1


@pytest.mark.asyncio
async def test_conversation_and_hydration_wrappers_share_one_hydrator() -> None:
    services = FakeServices()
    managed = _managed(services, MemoryStorage())

    runtime = managed.wrap_conversational(managed.wrap(ReportRuntime(write="notes")))
    events = [event async for event in runtime.stream({"messages": []})]

    snapshots = [
        event.payload
        for event in events
        if isinstance(event, UiPathRuntimeConversationMetaEvent)
    ]
    assert [f["path"] for f in snapshots[0]["workspaceFiles"]] == ["report.md"]
    assert services.built == 1
    assert services.attachments.uploads == 1


@pytest.mark.asyncio
async def test_creates_and_removes_its_own_workspace() -> None:
    runtime = _managed(FakeServices(), MemoryStorage())
    wrapped = runtime.wrap(ReportRuntime())

    assert runtime.workspace.path.exists()
    await wrapped.dispose()
    assert not runtime.workspace.path.exists()


@pytest.mark.asyncio
async def test_keeps_a_workspace_owned_by_the_caller(tmp_path: Path) -> None:
    owned = Workspace(tmp_path / "owned", cleanup=False)
    owned.path.mkdir()
    inner = ReportRuntime(write="draft")

    wrapped = _managed(FakeServices(), MemoryStorage(), workspace=owned).wrap(inner)
    await wrapped.execute()
    await wrapped.dispose()

    assert inner.workspace_path == owned.path
    assert (owned.path / "report.md").read_text() == "draft"


def test_wrapped_runtime_carries_the_runtime_id() -> None:
    wrapped = _managed(FakeServices(), MemoryStorage()).wrap(ReportRuntime())

    assert wrapped.runtime_id == "runtime-1"


def test_hydration_needs_a_registry_store() -> None:
    managed = ManagedWorkspace(
        services=FakeServices(), runtime_id="runtime-1", job_key=JOB_KEY
    )

    with pytest.raises(ValueError, match="registry store"):
        managed.wrap(ReportRuntime())
