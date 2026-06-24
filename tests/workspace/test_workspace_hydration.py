from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Any, AsyncGenerator

import pytest

from uipath.runtime import (
    HydrationPolicy,
    HydrationRuntime,
    UiPathExecuteOptions,
    UiPathRuntimeResult,
    UiPathRuntimeStatus,
    UiPathStreamOptions,
    Workspace,
    WorkspaceHydrator,
    WorkspaceRegistryStore,
)
from uipath.runtime.events import UiPathRuntimeEvent, UiPathRuntimeStateEvent
from uipath.runtime.schema import UiPathRuntimeSchema


class MemoryStorage:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str, str], Any] = {}

    async def set_value(
        self, runtime_id: str, namespace: str, key: str, value: Any
    ) -> None:
        self.values[(runtime_id, namespace, key)] = value

    async def get_value(self, runtime_id: str, namespace: str, key: str) -> Any:
        return self.values.get((runtime_id, namespace, key))


class FakeAttachments:
    def __init__(self) -> None:
        self.files: dict[uuid.UUID, tuple[str, bytes]] = {}
        self.uploads = 0
        self.downloads = 0

    async def upload_async(
        self,
        *,
        name: str,
        content: str | bytes | None = None,
        source_path: str | None = None,
        folder_key: str | None = None,
        folder_path: str | None = None,
    ) -> uuid.UUID:
        assert source_path is not None
        key = uuid.uuid4()
        self.files[key] = (name, Path(source_path).read_bytes())
        self.uploads += 1
        return key

    async def download_async(
        self,
        *,
        key: uuid.UUID,
        destination_path: str,
        folder_key: str | None = None,
        folder_path: str | None = None,
    ) -> str:
        name, content = self.files[key]
        Path(destination_path).write_bytes(content)
        self.downloads += 1
        return name


class FakeJobs:
    def __init__(self) -> None:
        self.attachments: dict[str, list[str]] = {}
        self.links: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def list_attachments_async(
        self,
        *,
        job_key: uuid.UUID,
        folder_key: str | None = None,
        folder_path: str | None = None,
    ) -> list[str]:
        return self.attachments.get(str(job_key), [])

    async def link_attachment_async(
        self,
        *,
        job_key: uuid.UUID,
        attachment_key: uuid.UUID,
        folder_key: str | None = None,
        folder_path: str | None = None,
    ) -> None:
        self.links.append((job_key, attachment_key))


class WritingRuntime:
    def __init__(self, workspace_path: Path, status: UiPathRuntimeStatus) -> None:
        self.workspace_path = workspace_path
        self.status = status
        self.disposed = False

    async def execute(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathExecuteOptions | None = None,
    ) -> UiPathRuntimeResult:
        (self.workspace_path / "notes.txt").write_text("hello", encoding="utf-8")
        return UiPathRuntimeResult(status=self.status, output={"ok": True})

    async def stream(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathStreamOptions | None = None,
    ) -> AsyncGenerator[UiPathRuntimeEvent, None]:
        yield UiPathRuntimeStateEvent(payload={"started": True})
        result = await self.execute(input, options)
        yield result

    async def get_schema(self) -> UiPathRuntimeSchema:
        raise NotImplementedError

    async def dispose(self) -> None:
        self.disposed = True


@pytest.mark.asyncio
async def test_dehydrate_uploads_changed_files_and_saves_registry(
    tmp_path: Path,
) -> None:
    workspace = Workspace.create(tmp_path / "workspace")
    attachments = FakeAttachments()
    jobs = FakeJobs()
    current_job = uuid.uuid4()
    hydrator = WorkspaceHydrator(
        workspace_path=workspace.path,
        attachments=attachments,
        jobs=jobs,
        current_job_key=str(current_job),
    )
    storage = MemoryStorage()
    store = WorkspaceRegistryStore(storage, "runtime-1")
    runtime = HydrationRuntime(
        WritingRuntime(workspace.path, UiPathRuntimeStatus.SUSPENDED),
        workspace=workspace,
        hydrator=hydrator,
        registry_store=store,
    )

    result = await runtime.execute({})

    registry = await store.load()
    assert result.status == UiPathRuntimeStatus.SUSPENDED
    assert list(registry) == ["notes.txt"]
    assert registry["notes.txt"]["attachment_name"] == ".uipath-workspace~1notes.txt"
    assert attachments.uploads == 1
    assert len(jobs.links) == 1


@pytest.mark.asyncio
async def test_successful_completion_persists_when_policy_allows(
    tmp_path: Path,
) -> None:
    workspace = Workspace.create(tmp_path / "workspace")
    attachments = FakeAttachments()
    storage = MemoryStorage()
    runtime = HydrationRuntime(
        WritingRuntime(workspace.path, UiPathRuntimeStatus.SUCCESSFUL),
        workspace=workspace,
        hydrator=WorkspaceHydrator(
            workspace_path=workspace.path,
            attachments=attachments,
        ),
        registry_store=WorkspaceRegistryStore(storage, "runtime-1"),
        policy=HydrationPolicy.SUSPEND_OR_SUCCESS,
    )

    await runtime.execute({})

    assert attachments.uploads == 1
    assert "notes.txt" in await runtime.registry_store.load()


@pytest.mark.asyncio
async def test_hydrate_downloads_missing_registry_files(tmp_path: Path) -> None:
    workspace = Workspace.create(tmp_path / "workspace")
    attachments = FakeAttachments()
    key = uuid.uuid4()
    attachments.files[key] = (".uipath-workspace/notes.txt", b"from attachment")
    hydrator = WorkspaceHydrator(
        workspace_path=workspace.path,
        attachments=attachments,
    )
    registry = {
        "notes.txt": {
            "attachment_key": str(key),
            "sha256": "different",
            "size": 15,
            "uploaded_at": "2026-01-01T00:00:00+00:00",
            "attachment_name": ".uipath-workspace/notes.txt",
        }
    }

    await hydrator.hydrate(registry)

    assert (workspace.path / "notes.txt").read_text(
        encoding="utf-8"
    ) == "from attachment"
    assert attachments.downloads == 1


@pytest.mark.asyncio
async def test_stream_persists_on_suspend(tmp_path: Path) -> None:
    workspace = Workspace.create(tmp_path / "workspace")
    attachments = FakeAttachments()
    storage = MemoryStorage()
    runtime = HydrationRuntime(
        WritingRuntime(workspace.path, UiPathRuntimeStatus.SUSPENDED),
        workspace=workspace,
        hydrator=WorkspaceHydrator(
            workspace_path=workspace.path,
            attachments=attachments,
        ),
        registry_store=WorkspaceRegistryStore(storage, "runtime-1"),
    )

    events = [event async for event in runtime.stream({})]

    assert isinstance(events[-1], UiPathRuntimeResult)
    assert attachments.uploads == 1
    assert "notes.txt" in await runtime.registry_store.load()


@pytest.mark.asyncio
async def test_hydrate_skips_unchanged_local_file(tmp_path: Path) -> None:
    workspace = Workspace.create(tmp_path / "workspace")
    attachments = FakeAttachments()
    (workspace.path / "notes.txt").write_text("same", encoding="utf-8")
    hydrator = WorkspaceHydrator(
        workspace_path=workspace.path,
        attachments=attachments,
    )
    digest = hydrator._sha256(workspace.path / "notes.txt")
    registry = {
        "notes.txt": {
            "attachment_key": str(uuid.uuid4()),
            "sha256": digest,
            "size": 4,
            "uploaded_at": "",
            "attachment_name": ".uipath-workspace~1notes.txt",
        }
    }

    await hydrator.hydrate(registry)

    assert attachments.downloads == 0


@pytest.mark.asyncio
async def test_workspace_dispose_removes_temp_dir(tmp_path: Path) -> None:
    workspace = Workspace.create(tmp_path / "workspace", cleanup=True)
    (workspace.path / "file.txt").write_text("x", encoding="utf-8")

    await workspace.dispose()

    assert not workspace.path.exists()


@pytest.mark.asyncio
async def test_workspace_dispose_keeps_owned_path_by_default(tmp_path: Path) -> None:
    workspace = Workspace.create(tmp_path / "workspace")
    (workspace.path / "file.txt").write_text("x", encoding="utf-8")

    await workspace.dispose()

    assert workspace.path.exists()
    shutil.rmtree(workspace.path)


@pytest.mark.asyncio
async def test_create_temp_workspace_is_cleaned_up_by_default() -> None:
    workspace = Workspace.create()

    assert workspace.path.exists()

    await workspace.dispose()

    assert not workspace.path.exists()


@pytest.mark.asyncio
async def test_dehydrate_relinks_unchanged_file_without_reupload(
    tmp_path: Path,
) -> None:
    workspace = Workspace.create(tmp_path / "workspace")
    attachments = FakeAttachments()
    jobs = FakeJobs()
    current_job = uuid.uuid4()
    key = uuid.uuid4()
    (workspace.path / "notes.txt").write_text("same", encoding="utf-8")
    hydrator = WorkspaceHydrator(
        workspace_path=workspace.path,
        attachments=attachments,
        jobs=jobs,
        current_job_key=str(current_job),
    )
    prior = {
        "notes.txt": {
            "attachment_key": str(key),
            "sha256": hydrator._sha256(workspace.path / "notes.txt"),
            "size": 4,
            "uploaded_at": "",
            "attachment_name": ".uipath-workspace~1notes.txt",
        }
    }

    result = await hydrator.dehydrate(prior)

    assert attachments.uploads == 0
    assert result["notes.txt"]["attachment_key"] == str(key)
    assert jobs.links == [(current_job, key)]


@pytest.mark.asyncio
async def test_attachment_names_are_single_segment_for_nested_files(
    tmp_path: Path,
) -> None:
    """Attachment names must stay slash-free (a "/" breaks the blob round-trip)."""
    workspace = Workspace.create(tmp_path / "workspace")
    (workspace.path / "plan").mkdir(parents=True, exist_ok=True)
    (workspace.path / "plan" / "todo.md").write_text("step 1", encoding="utf-8")
    attachments = FakeAttachments()
    hydrator = WorkspaceHydrator(
        workspace_path=workspace.path,
        attachments=attachments,
    )

    registry = await hydrator.dehydrate({})

    assert "plan/todo.md" in registry
    attachment_name = registry["plan/todo.md"]["attachment_name"]
    assert "/" not in attachment_name
    assert all("/" not in name for name, _ in attachments.files.values())
    assert (
        hydrator._virtual_path_from_attachment_name(attachment_name) == "plan/todo.md"
    )


@pytest.mark.asyncio
async def test_attachment_name_round_trips_special_characters(tmp_path: Path) -> None:
    """The encoding must be reversible even for paths containing the escape char."""
    workspace = Workspace.create(tmp_path / "workspace")
    hydrator = WorkspaceHydrator(
        workspace_path=workspace.path,
        attachments=FakeAttachments(),
    )

    for virtual_path in ["a~~b.txt", "plan/todo.md", "we~ird/a~~b/file.txt"]:
        name = hydrator._attachment_name_for_virtual_path(virtual_path)
        assert "/" not in name
        assert hydrator._virtual_path_from_attachment_name(name) == virtual_path


@pytest.mark.asyncio
async def test_dehydrate_drops_files_deleted_locally(tmp_path: Path) -> None:
    """A registry entry whose file no longer exists is not carried forward."""
    workspace = Workspace.create(tmp_path / "workspace")
    attachments = FakeAttachments()
    hydrator = WorkspaceHydrator(
        workspace_path=workspace.path,
        attachments=attachments,
    )
    (workspace.path / "present.txt").write_text("hi", encoding="utf-8")
    prior = {
        "gone.txt": {
            "attachment_key": str(uuid.uuid4()),
            "sha256": "stale",
            "size": 1,
            "uploaded_at": "2026-01-01T00:00:00+00:00",
            "attachment_name": ".uipath-workspace~1gone.txt",
        }
    }

    result = await hydrator.dehydrate(prior)

    assert "present.txt" in result
    assert "gone.txt" not in result


class _WriteThenRaiseRuntime:
    def __init__(self, workspace_path: Path) -> None:
        self.workspace_path = workspace_path

    async def execute(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathExecuteOptions | None = None,
    ) -> UiPathRuntimeResult:
        (self.workspace_path / "partial.txt").write_text("wip", encoding="utf-8")
        raise RuntimeError("boom")

    async def stream(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathStreamOptions | None = None,
    ) -> AsyncGenerator[UiPathRuntimeEvent, None]:
        await self.execute(input, options)
        yield UiPathRuntimeStateEvent(payload={})

    async def get_schema(self) -> UiPathRuntimeSchema:
        raise NotImplementedError

    async def dispose(self) -> None:
        pass


@pytest.mark.asyncio
async def test_always_policy_persists_on_failure(tmp_path: Path) -> None:
    workspace = Workspace.create(tmp_path / "workspace")
    attachments = FakeAttachments()
    storage = MemoryStorage()
    runtime = HydrationRuntime(
        _WriteThenRaiseRuntime(workspace.path),
        workspace=workspace,
        hydrator=WorkspaceHydrator(
            workspace_path=workspace.path,
            attachments=attachments,
        ),
        registry_store=WorkspaceRegistryStore(storage, "runtime-1"),
        policy=HydrationPolicy.ALWAYS,
    )

    with pytest.raises(RuntimeError):
        await runtime.execute({})

    assert attachments.uploads == 1
    assert "partial.txt" in await runtime.registry_store.load()


@pytest.mark.asyncio
async def test_suspend_only_policy_skips_persist_on_failure(tmp_path: Path) -> None:
    workspace = Workspace.create(tmp_path / "workspace")
    attachments = FakeAttachments()
    storage = MemoryStorage()
    runtime = HydrationRuntime(
        _WriteThenRaiseRuntime(workspace.path),
        workspace=workspace,
        hydrator=WorkspaceHydrator(
            workspace_path=workspace.path,
            attachments=attachments,
        ),
        registry_store=WorkspaceRegistryStore(storage, "runtime-1"),
        policy=HydrationPolicy.SUSPEND_ONLY,
    )

    with pytest.raises(RuntimeError):
        await runtime.execute({})

    assert attachments.uploads == 0


@pytest.mark.asyncio
async def test_always_policy_persists_on_stream_failure(tmp_path: Path) -> None:
    workspace = Workspace.create(tmp_path / "workspace")
    attachments = FakeAttachments()
    storage = MemoryStorage()
    runtime = HydrationRuntime(
        _WriteThenRaiseRuntime(workspace.path),
        workspace=workspace,
        hydrator=WorkspaceHydrator(
            workspace_path=workspace.path,
            attachments=attachments,
        ),
        registry_store=WorkspaceRegistryStore(storage, "runtime-1"),
        policy=HydrationPolicy.ALWAYS,
    )

    with pytest.raises(RuntimeError):
        async for _ in runtime.stream({}):
            pass

    assert attachments.uploads == 1
