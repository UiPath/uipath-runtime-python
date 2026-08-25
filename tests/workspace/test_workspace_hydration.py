from __future__ import annotations

import asyncio
import shutil
import uuid
from pathlib import Path
from typing import Any, AsyncGenerator

import pytest
from uipath.core.chat import (
    UiPathConversationMessageEndEvent,
    UiPathConversationMessageEvent,
)

from uipath.runtime import (
    ConversationalWorkspaceRuntime,
    HydrationPolicy,
    HydrationRuntime,
    UiPathExecuteOptions,
    UiPathRuntimeResult,
    UiPathRuntimeStatus,
    UiPathStreamOptions,
    Workspace,
    WorkspaceHydrator,
    WorkspaceRegistryStore,
    get_workspace_path,
)
from uipath.runtime.errors import UiPathErrorCategory, UiPathRuntimeError
from uipath.runtime.events import (
    UiPathRuntimeConversationMetaEvent,
    UiPathRuntimeEvent,
    UiPathRuntimeMessageEvent,
    UiPathRuntimeStateEvent,
)
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


class FailingDownloadAttachments(FakeAttachments):
    async def download_async(
        self,
        *,
        key: uuid.UUID,
        destination_path: str,
        folder_key: str | None = None,
        folder_path: str | None = None,
    ) -> str:
        raise RuntimeError("download failed")


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
        linked_attachments = self.attachments.setdefault(str(job_key), [])
        if str(attachment_key) in linked_attachments:
            raise RuntimeError("The association already exists")
        linked_attachments.append(str(attachment_key))
        self.links.append((job_key, attachment_key))


@pytest.mark.asyncio
async def test_registry_store_distinguishes_missing_from_saved_empty() -> None:
    store = WorkspaceRegistryStore(MemoryStorage(), "runtime-1")

    assert await store.try_load() is None

    await store.save({})

    assert await store.try_load() == {}
    assert await store.load() == {}


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


class SchemaRuntime(WritingRuntime):
    def __init__(self, workspace_path: Path) -> None:
        super().__init__(workspace_path, UiPathRuntimeStatus.SUCCESSFUL)

    async def get_schema(self) -> UiPathRuntimeSchema:
        return UiPathRuntimeSchema(
            filePath="agent.py",
            uniqueId="agent",
            type="agent",
            input={},
            output={},
        )


class ContextAwareRuntime(WritingRuntime):
    async def execute(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathExecuteOptions | None = None,
    ) -> UiPathRuntimeResult:
        assert get_workspace_path() == self.workspace_path
        return await super().execute(input, options)

    async def stream(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathStreamOptions | None = None,
    ) -> AsyncGenerator[UiPathRuntimeEvent, None]:
        assert get_workspace_path() == self.workspace_path
        async for event in super().stream(input, options):
            yield event


class ChildTaskRuntime(WritingRuntime):
    def __init__(self, workspace_path: Path, release: asyncio.Event) -> None:
        super().__init__(workspace_path, UiPathRuntimeStatus.SUCCESSFUL)
        self.release = release
        self.task: asyncio.Task[Path] | None = None

    async def execute(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathExecuteOptions | None = None,
    ) -> UiPathRuntimeResult:
        async def access_workspace_after_execution() -> Path:
            await self.release.wait()
            return get_workspace_path()

        self.task = asyncio.create_task(access_workspace_after_execution())
        return await super().execute(input, options)


class StreamingChildTaskRuntime(WritingRuntime):
    def __init__(self, workspace_path: Path, release: asyncio.Event) -> None:
        super().__init__(workspace_path, UiPathRuntimeStatus.SUCCESSFUL)
        self.release = release
        self.task: asyncio.Task[Path] | None = None

    async def stream(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathStreamOptions | None = None,
    ) -> AsyncGenerator[UiPathRuntimeEvent, None]:
        async def access_workspace_during_stream() -> Path:
            await self.release.wait()
            return get_workspace_path()

        self.task = asyncio.create_task(access_workspace_during_stream())
        yield UiPathRuntimeStateEvent(payload={"started": True})
        await self.release.wait()
        assert self.task is not None
        assert await self.task == self.workspace_path
        yield UiPathRuntimeResult(status=self.status, output={"ok": True})


class FinalChildTaskRuntime(WritingRuntime):
    def __init__(self, workspace_path: Path, release: asyncio.Event) -> None:
        super().__init__(workspace_path, UiPathRuntimeStatus.SUCCESSFUL)
        self.release = release
        self.task: asyncio.Task[Path] | None = None

    async def stream(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathStreamOptions | None = None,
    ) -> AsyncGenerator[UiPathRuntimeEvent, None]:
        async def access_workspace_after_final_result() -> Path:
            await self.release.wait()
            return get_workspace_path()

        self.task = asyncio.create_task(access_workspace_after_final_result())
        yield UiPathRuntimeStateEvent(payload={"started": True})
        yield UiPathRuntimeResult(status=self.status, output={"ok": True})


class ReadingRuntime(WritingRuntime):
    async def execute(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathExecuteOptions | None = None,
    ) -> UiPathRuntimeResult:
        assert (self.workspace_path / "notes.txt").read_text(
            encoding="utf-8"
        ) == "hello"
        (self.workspace_path / "notes.txt").write_text(
            "hello after resume", encoding="utf-8"
        )
        return UiPathRuntimeResult(status=self.status, output={"ok": True})

    async def stream(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathStreamOptions | None = None,
    ) -> AsyncGenerator[UiPathRuntimeEvent, None]:
        message_id = "assistant-1"
        yield UiPathRuntimeMessageEvent(
            payload=UiPathConversationMessageEvent(
                message_id=message_id,
                end=UiPathConversationMessageEndEvent(),
            )
        )
        yield await self.execute(input, options)


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


def test_get_workspace_path_requires_a_managed_execution() -> None:
    with pytest.raises(UiPathRuntimeError, match="No managed workspace") as error:
        get_workspace_path()

    assert error.value.error_info.code == "Python.MANAGED_WORKSPACE_UNAVAILABLE"
    assert "not from module-level initialization" in str(error.value)
    assert error.value.error_info.category == UiPathErrorCategory.USER


@pytest.mark.asyncio
async def test_workspace_path_is_available_only_during_execution(
    tmp_path: Path,
) -> None:
    workspace = Workspace.create(tmp_path / "workspace")
    runtime = HydrationRuntime(
        ContextAwareRuntime(workspace.path, UiPathRuntimeStatus.SUCCESSFUL),
        workspace=workspace,
        hydrator=WorkspaceHydrator(
            workspace_path=workspace.path,
            attachments=FakeAttachments(),
        ),
        registry_store=WorkspaceRegistryStore(MemoryStorage(), "runtime-1"),
    )

    await runtime.execute({})

    with pytest.raises(UiPathRuntimeError, match="No managed workspace"):
        get_workspace_path()


@pytest.mark.asyncio
async def test_workspace_path_is_available_during_streaming(
    tmp_path: Path,
) -> None:
    workspace = Workspace.create(tmp_path / "workspace")
    runtime = HydrationRuntime(
        ContextAwareRuntime(workspace.path, UiPathRuntimeStatus.SUCCESSFUL),
        workspace=workspace,
        hydrator=WorkspaceHydrator(
            workspace_path=workspace.path,
            attachments=FakeAttachments(),
        ),
        registry_store=WorkspaceRegistryStore(MemoryStorage(), "runtime-1"),
    )

    events = [event async for event in runtime.stream({})]

    assert isinstance(events[-1], UiPathRuntimeResult)
    with pytest.raises(UiPathRuntimeError, match="No managed workspace"):
        get_workspace_path()


@pytest.mark.asyncio
async def test_stream_does_not_expose_workspace_to_the_consumer(
    tmp_path: Path,
) -> None:
    workspace = Workspace.create(tmp_path / "workspace")
    runtime = HydrationRuntime(
        ContextAwareRuntime(workspace.path, UiPathRuntimeStatus.SUCCESSFUL),
        workspace=workspace,
        hydrator=WorkspaceHydrator(
            workspace_path=workspace.path,
            attachments=FakeAttachments(),
        ),
        registry_store=WorkspaceRegistryStore(MemoryStorage(), "runtime-1"),
    )

    stream = runtime.stream({})
    assert isinstance(await anext(stream), UiPathRuntimeStateEvent)
    with pytest.raises(UiPathRuntimeError, match="No managed workspace"):
        get_workspace_path()

    await stream.aclose()
    with pytest.raises(UiPathRuntimeError, match="No managed workspace"):
        get_workspace_path()


@pytest.mark.asyncio
async def test_breaking_after_the_final_stream_result_resets_workspace_access(
    tmp_path: Path,
) -> None:
    workspace = Workspace.create(tmp_path / "workspace")
    runtime = HydrationRuntime(
        ContextAwareRuntime(workspace.path, UiPathRuntimeStatus.SUCCESSFUL),
        workspace=workspace,
        hydrator=WorkspaceHydrator(
            workspace_path=workspace.path,
            attachments=FakeAttachments(),
        ),
        registry_store=WorkspaceRegistryStore(MemoryStorage(), "runtime-1"),
    )

    stream = runtime.stream({})
    async for event in stream:
        if isinstance(event, UiPathRuntimeResult):
            break

    await stream.aclose()

    with pytest.raises(UiPathRuntimeError, match="No managed workspace"):
        get_workspace_path()


@pytest.mark.asyncio
async def test_stream_supports_task_wrapped_iterator_resumes(
    tmp_path: Path,
) -> None:
    workspace = Workspace.create(tmp_path / "workspace")
    runtime = HydrationRuntime(
        ContextAwareRuntime(workspace.path, UiPathRuntimeStatus.SUCCESSFUL),
        workspace=workspace,
        hydrator=WorkspaceHydrator(
            workspace_path=workspace.path,
            attachments=FakeAttachments(),
        ),
        registry_store=WorkspaceRegistryStore(MemoryStorage(), "runtime-1"),
    )

    stream = runtime.stream({})
    first = await asyncio.wait_for(anext(stream), timeout=1)
    result = await asyncio.wait_for(anext(stream), timeout=1)

    assert isinstance(first, UiPathRuntimeStateEvent)
    assert isinstance(result, UiPathRuntimeResult)
    await stream.aclose()


@pytest.mark.asyncio
async def test_stream_child_tasks_keep_workspace_access_between_events(
    tmp_path: Path,
) -> None:
    workspace = Workspace.create(tmp_path / "workspace")
    release = asyncio.Event()
    delegate = StreamingChildTaskRuntime(workspace.path, release)
    runtime = HydrationRuntime(
        delegate,
        workspace=workspace,
        hydrator=WorkspaceHydrator(
            workspace_path=workspace.path,
            attachments=FakeAttachments(),
        ),
        registry_store=WorkspaceRegistryStore(MemoryStorage(), "runtime-1"),
    )

    stream = runtime.stream({})
    assert isinstance(await anext(stream), UiPathRuntimeStateEvent)
    release.set()
    assert isinstance(await anext(stream), UiPathRuntimeResult)
    await stream.aclose()


@pytest.mark.asyncio
async def test_stream_does_not_retry_failed_final_persistence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = Workspace.create(tmp_path / "workspace")
    runtime = HydrationRuntime(
        WritingRuntime(workspace.path, UiPathRuntimeStatus.SUCCESSFUL),
        workspace=workspace,
        hydrator=WorkspaceHydrator(
            workspace_path=workspace.path,
            attachments=FakeAttachments(),
        ),
        registry_store=WorkspaceRegistryStore(MemoryStorage(), "runtime-1"),
        policy=HydrationPolicy.ALWAYS,
    )
    persist_calls = 0

    async def fail_persist() -> None:
        nonlocal persist_calls
        persist_calls += 1
        raise RuntimeError("persist failed")

    monkeypatch.setattr(runtime, "_persist", fail_persist)

    with pytest.raises(RuntimeError, match="persist failed"):
        [event async for event in runtime.stream({})]

    assert persist_calls == 1


@pytest.mark.asyncio
async def test_stream_hydration_failure_preserves_the_workspace_registry(
    tmp_path: Path,
) -> None:
    workspace = Workspace.create(tmp_path / "workspace")
    storage = MemoryStorage()
    store = WorkspaceRegistryStore(storage, "runtime-1")
    registry = {
        "notes.txt": {
            "attachment_key": str(uuid.uuid4()),
            "sha256": "0" * 64,
            "size": 1,
            "uploaded_at": "2026-01-01T00:00:00+00:00",
        }
    }
    await store.save(registry)
    runtime = HydrationRuntime(
        WritingRuntime(workspace.path, UiPathRuntimeStatus.SUCCESSFUL),
        workspace=workspace,
        hydrator=WorkspaceHydrator(
            workspace_path=workspace.path,
            attachments=FailingDownloadAttachments(),
        ),
        registry_store=store,
        policy=HydrationPolicy.ALWAYS,
    )

    with pytest.raises(RuntimeError, match="download failed"):
        [event async for event in runtime.stream({})]

    assert await store.load() == registry


@pytest.mark.asyncio
async def test_stream_revokes_workspace_before_yielding_final_result(
    tmp_path: Path,
) -> None:
    workspace = Workspace.create(tmp_path / "workspace")
    release = asyncio.Event()
    delegate = FinalChildTaskRuntime(workspace.path, release)
    runtime = HydrationRuntime(
        delegate,
        workspace=workspace,
        hydrator=WorkspaceHydrator(
            workspace_path=workspace.path,
            attachments=FakeAttachments(),
        ),
        registry_store=WorkspaceRegistryStore(MemoryStorage(), "runtime-1"),
    )

    stream = runtime.stream({})
    assert isinstance(await anext(stream), UiPathRuntimeStateEvent)
    assert isinstance(await anext(stream), UiPathRuntimeResult)
    release.set()

    assert delegate.task is not None
    with pytest.raises(UiPathRuntimeError, match="No managed workspace"):
        await delegate.task


@pytest.mark.asyncio
async def test_child_tasks_cannot_access_workspace_after_execution(
    tmp_path: Path,
) -> None:
    workspace = Workspace.create(tmp_path / "workspace")
    release = asyncio.Event()
    delegate = ChildTaskRuntime(workspace.path, release)
    runtime = HydrationRuntime(
        delegate,
        workspace=workspace,
        hydrator=WorkspaceHydrator(
            workspace_path=workspace.path,
            attachments=FakeAttachments(),
        ),
        registry_store=WorkspaceRegistryStore(MemoryStorage(), "runtime-1"),
    )

    await runtime.execute({})
    release.set()

    assert delegate.task is not None
    with pytest.raises(UiPathRuntimeError, match="No managed workspace"):
        await delegate.task


@pytest.mark.asyncio
async def test_hydrator_factory_is_deferred_and_cached(tmp_path: Path) -> None:
    workspace = Workspace.create(tmp_path / "workspace")
    attachments = FakeAttachments()
    storage = MemoryStorage()
    factory_calls = 0

    def create_hydrator() -> WorkspaceHydrator:
        nonlocal factory_calls
        factory_calls += 1
        return WorkspaceHydrator(
            workspace_path=workspace.path,
            attachments=attachments,
        )

    runtime = HydrationRuntime(
        SchemaRuntime(workspace.path),
        workspace=workspace,
        hydrator_factory=create_hydrator,
        registry_store=WorkspaceRegistryStore(storage, "runtime-1"),
    )

    await runtime.get_schema()
    assert factory_calls == 0
    assert runtime.hydrator is None

    await runtime.execute({})
    assert [event async for event in runtime.stream({})]

    assert factory_calls == 1
    assert runtime.hydrator is not None
    assert runtime.hydrator.attachments is attachments


@pytest.mark.asyncio
async def test_dispose_does_not_create_hydrator(tmp_path: Path) -> None:
    workspace = Workspace.create(tmp_path / "workspace")
    factory_calls = 0

    def create_hydrator() -> WorkspaceHydrator:
        nonlocal factory_calls
        factory_calls += 1
        return WorkspaceHydrator(
            workspace_path=workspace.path,
            attachments=FakeAttachments(),
        )

    delegate = SchemaRuntime(workspace.path)
    runtime = HydrationRuntime(
        delegate,
        workspace=workspace,
        hydrator_factory=create_hydrator,
        registry_store=WorkspaceRegistryStore(MemoryStorage(), "runtime-1"),
    )

    await runtime.dispose()

    assert delegate.disposed
    assert factory_calls == 0


def test_hydration_runtime_requires_exactly_one_hydrator_source(
    tmp_path: Path,
) -> None:
    workspace = Workspace.create(tmp_path / "workspace")
    hydrator = WorkspaceHydrator(
        workspace_path=workspace.path,
        attachments=FakeAttachments(),
    )
    registry_store = WorkspaceRegistryStore(MemoryStorage(), "runtime-1")

    with pytest.raises(ValueError, match="exactly one"):
        HydrationRuntime(  # type: ignore[call-overload]
            WritingRuntime(workspace.path, UiPathRuntimeStatus.SUCCESSFUL),
            workspace=workspace,
            registry_store=registry_store,
        )

    with pytest.raises(ValueError, match="exactly one"):
        HydrationRuntime(  # type: ignore[call-overload]
            WritingRuntime(workspace.path, UiPathRuntimeStatus.SUCCESSFUL),
            workspace=workspace,
            hydrator=hydrator,
            hydrator_factory=lambda: hydrator,
            registry_store=registry_store,
        )


@pytest.mark.asyncio
async def test_conversational_resume_reuses_registry_persisted_on_suspend(
    tmp_path: Path,
) -> None:
    workspace = Workspace.create(tmp_path / "workspace")
    attachments = FakeAttachments()
    store = WorkspaceRegistryStore(MemoryStorage(), "runtime-1")
    hydrator = WorkspaceHydrator(
        workspace_path=workspace.path,
        attachments=attachments,
    )
    delegate = WritingRuntime(workspace.path, UiPathRuntimeStatus.SUSPENDED)
    hydration_runtime = HydrationRuntime(
        delegate,
        workspace=workspace,
        hydrator=hydrator,
        registry_store=store,
    )
    runtime = ConversationalWorkspaceRuntime(
        hydration_runtime,
        hydrator=hydrator,
        registry_store=store,
    )

    await runtime.execute({})
    delegate.status = UiPathRuntimeStatus.SUCCESSFUL
    await runtime.execute({}, options=UiPathExecuteOptions(resume=True))

    assert attachments.uploads == 1


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
async def test_hydrate_compatibility_api_downloads_registry_files(
    tmp_path: Path,
) -> None:
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
async def test_conversational_suspend_restores_in_new_workspace(tmp_path: Path) -> None:
    attachments = FakeAttachments()
    storage = MemoryStorage()
    first_workspace = Workspace.create(tmp_path / "first", cleanup=False)
    first_hydrator = WorkspaceHydrator(
        workspace_path=first_workspace.path,
        attachments=attachments,
    )
    suspended_runtime = HydrationRuntime(
        WritingRuntime(first_workspace.path, UiPathRuntimeStatus.SUSPENDED),
        workspace=first_workspace,
        hydrator=first_hydrator,
        registry_store=WorkspaceRegistryStore(storage, "runtime-1"),
    )
    conversation_runtime = ConversationalWorkspaceRuntime(
        suspended_runtime,
        hydrator=first_hydrator,
    )

    events = [event async for event in conversation_runtime.stream({"messages": []})]

    assert isinstance(events[-1], UiPathRuntimeResult)
    assert attachments.uploads == 1

    second_workspace = Workspace.create(tmp_path / "second", cleanup=False)
    second_hydrator = WorkspaceHydrator(
        workspace_path=second_workspace.path,
        attachments=attachments,
    )
    second_registry_store = WorkspaceRegistryStore(storage, "runtime-1")
    resumed_runtime = HydrationRuntime(
        ReadingRuntime(second_workspace.path, UiPathRuntimeStatus.SUCCESSFUL),
        workspace=second_workspace,
        hydrator=second_hydrator,
        registry_store=second_registry_store,
    )
    resumed_conversation_runtime = ConversationalWorkspaceRuntime(
        resumed_runtime,
        hydrator=second_hydrator,
        registry_store=second_registry_store,
    )

    resumed_events = [
        event async for event in resumed_conversation_runtime.stream({"messages": []})
    ]

    assert isinstance(resumed_events[-1], UiPathRuntimeResult)
    assert attachments.downloads == 1
    assert attachments.uploads == 2
    workspace_snapshots = [
        event.payload
        for event in resumed_events
        if isinstance(event, UiPathRuntimeConversationMetaEvent)
    ]
    assert len(workspace_snapshots) == 1
    assert workspace_snapshots[0]["workspaceFiles"][0]["path"] == "notes.txt"


@pytest.mark.asyncio
async def test_hydrate_from_registry_skips_unchanged_local_file(
    tmp_path: Path,
) -> None:
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

    await hydrator.hydrate_from_registry(registry)

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
async def test_dehydrate_does_not_relink_attachment_already_linked_to_job(
    tmp_path: Path,
) -> None:
    workspace = Workspace.create(tmp_path / "workspace")
    attachments = FakeAttachments()
    jobs = FakeJobs()
    current_job = uuid.uuid4()
    (workspace.path / "notes.txt").write_text("same", encoding="utf-8")

    first_hydrator = WorkspaceHydrator(
        workspace_path=workspace.path,
        attachments=attachments,
        jobs=jobs,
        current_job_key=str(current_job),
    )
    registry = await first_hydrator.dehydrate({})

    resumed_hydrator = WorkspaceHydrator(
        workspace_path=workspace.path,
        attachments=attachments,
        jobs=jobs,
        current_job_key=str(current_job),
    )
    result = await resumed_hydrator.dehydrate(registry)

    assert result == registry
    assert attachments.uploads == 1
    assert jobs.links == [
        (current_job, uuid.UUID(registry["notes.txt"]["attachment_key"]))
    ]


@pytest.mark.asyncio
async def test_link_attachment_does_not_relink_when_already_associated(
    tmp_path: Path,
) -> None:
    workspace = Workspace.create(tmp_path / "workspace")
    jobs = FakeJobs()
    current_job = uuid.uuid4()
    attachment_key = uuid.uuid4()
    hydrator = WorkspaceHydrator(
        workspace_path=workspace.path,
        attachments=FakeAttachments(),
        jobs=jobs,
        current_job_key=str(current_job),
    )

    await hydrator.link_attachment(str(attachment_key))
    await hydrator.link_attachment(str(attachment_key))

    assert jobs.links == [(current_job, attachment_key)]


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
