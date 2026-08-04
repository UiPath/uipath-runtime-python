from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, AsyncGenerator
from unittest.mock import AsyncMock

import pytest
from uipath.core.chat import UiPathConversationMessageEvent

from tests.workspace.test_workspace_hydration import FakeAttachments, MemoryStorage
from uipath.runtime import (
    ConversationalWorkspaceRuntime,
    HydrationRuntime,
    UiPathExecuteOptions,
    UiPathRuntimeResult,
    UiPathRuntimeStatus,
    UiPathStreamOptions,
    Workspace,
    WorkspaceHydrator,
    WorkspaceRegistryStore,
)
from uipath.runtime.events import (
    UiPathRuntimeConversationMetaEvent,
    UiPathRuntimeEvent,
    UiPathRuntimeMessageEvent,
)
from uipath.runtime.schema import UiPathRuntimeSchema
from uipath.runtime.workspace.conversational import (
    CONVERSATION_META_EVENTS_INPUT_KEY,
    WORKSPACE_FILES_META_KEY,
)


class ScriptedRuntime:
    def __init__(
        self,
        events: list[UiPathRuntimeEvent],
        result: UiPathRuntimeResult,
        workspace_path: Path | None = None,
        files: dict[str, str] | None = None,
    ) -> None:
        self.events = events
        self.result = result
        self.workspace_path = workspace_path
        self.files = files or {}
        self.disposed = False
        self.received_input: dict[str, Any] | None = None
        self.received_options: UiPathStreamOptions | None = None

    async def execute(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathExecuteOptions | None = None,
    ) -> UiPathRuntimeResult:
        raise NotImplementedError

    async def stream(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathStreamOptions | None = None,
    ) -> AsyncGenerator[UiPathRuntimeEvent, None]:
        self.received_input = input
        self.received_options = options
        if self.workspace_path is not None:
            for virtual_path, content in self.files.items():
                target = self.workspace_path / virtual_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
        for event in self.events:
            yield event
        yield self.result

    async def get_schema(self) -> UiPathRuntimeSchema:
        raise NotImplementedError

    async def dispose(self) -> None:
        self.disposed = True


def make_runtime(
    tmp_path: Path,
    delegate: ScriptedRuntime,
    attachments: FakeAttachments,
    registry_store: WorkspaceRegistryStore | None = None,
) -> tuple[ConversationalWorkspaceRuntime, Workspace]:
    workspace = Workspace.create(tmp_path / "workspace")
    return (
        ConversationalWorkspaceRuntime(
            delegate,
            hydrator=WorkspaceHydrator(
                workspace_path=workspace.path,
                attachments=attachments,
            ),
            registry_store=registry_store,
        ),
        workspace,
    )


def workspace_meta_event(
    files: list[tuple[str, uuid.UUID]],
    *,
    top_level: bool = False,
) -> dict[str, Any]:
    meta_event = {
        WORKSPACE_FILES_META_KEY: [
            {"path": path, "attachmentKey": str(attachment_key)}
            for path, attachment_key in files
        ]
    }
    event: dict[str, Any] = {"conversationId": "conversation-1"}
    if top_level:
        event["metaEvent"] = meta_event
    else:
        event["exchange"] = {
            "exchangeId": "exchange-1",
            "metaEvent": meta_event,
        }
    return event


def meta_events(
    events: list[UiPathRuntimeEvent],
) -> list[UiPathRuntimeConversationMetaEvent]:
    return [
        event
        for event in events
        if isinstance(event, UiPathRuntimeConversationMetaEvent)
    ]


@pytest.mark.asyncio
async def test_success_emits_sorted_workspace_snapshot_before_result(
    tmp_path: Path,
) -> None:
    attachments = FakeAttachments()
    delegate_message = UiPathRuntimeMessageEvent(
        payload=UiPathConversationMessageEvent(message_id="message-1")
    )
    delegate = ScriptedRuntime(
        events=[delegate_message],
        result=UiPathRuntimeResult(status=UiPathRuntimeStatus.SUCCESSFUL),
        workspace_path=tmp_path / "workspace",
        files={"z.txt": "z", "nested/a.txt": "a"},
    )
    runtime, _ = make_runtime(tmp_path, delegate, attachments)

    events = [event async for event in runtime.stream({"messages": []})]

    assert events[0] is delegate_message
    assert isinstance(events[-2], UiPathRuntimeConversationMetaEvent)
    assert isinstance(events[-1], UiPathRuntimeResult)
    assert [file["path"] for file in events[-2].payload[WORKSPACE_FILES_META_KEY]] == [
        "nested/a.txt",
        "z.txt",
    ]
    assert attachments.uploads == 2


@pytest.mark.asyncio
async def test_empty_workspace_emits_authoritative_snapshot(tmp_path: Path) -> None:
    delegate = ScriptedRuntime(
        events=[],
        result=UiPathRuntimeResult(status=UiPathRuntimeStatus.SUCCESSFUL),
    )
    runtime, _ = make_runtime(tmp_path, delegate, FakeAttachments())

    events = [event async for event in runtime.stream({"messages": []})]

    assert meta_events(events)[0].payload == {WORKSPACE_FILES_META_KEY: []}


@pytest.mark.asyncio
async def test_empty_snapshot_removes_stale_workspace_files(tmp_path: Path) -> None:
    attachments = FakeAttachments()
    delegate = ScriptedRuntime(
        events=[],
        result=UiPathRuntimeResult(status=UiPathRuntimeStatus.SUCCESSFUL),
    )
    runtime, workspace = make_runtime(tmp_path, delegate, attachments)
    stale_file = workspace.path / "stale.txt"
    stale_file.write_text("stale", encoding="utf-8")

    events = [
        event
        async for event in runtime.stream(
            {
                CONVERSATION_META_EVENTS_INPUT_KEY: [workspace_meta_event([])],
            }
        )
    ]

    assert not stale_file.exists()
    assert meta_events(events)[0].payload == {WORKSPACE_FILES_META_KEY: []}
    assert attachments.uploads == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "workspace_files",
    [
        "invalid",
        [{"path": "restored.txt"}],
        [
            {"path": "restored.txt", "attachmentKey": str(uuid.uuid4())},
            {"attachmentKey": str(uuid.uuid4())},
        ],
    ],
)
async def test_malformed_snapshot_does_not_remove_workspace_files(
    tmp_path: Path,
    workspace_files: object,
) -> None:
    attachments = FakeAttachments()
    delegate = ScriptedRuntime(
        events=[],
        result=UiPathRuntimeResult(status=UiPathRuntimeStatus.FAULTED),
    )
    runtime, workspace = make_runtime(tmp_path, delegate, attachments)
    existing_file = workspace.path / "existing.txt"
    existing_file.write_text("keep", encoding="utf-8")

    input = {
        CONVERSATION_META_EVENTS_INPUT_KEY: [
            {"exchange": {"metaEvent": {WORKSPACE_FILES_META_KEY: workspace_files}}}
        ]
    }
    async for _ in runtime.stream(input):
        pass

    assert existing_file.read_text(encoding="utf-8") == "keep"
    assert attachments.downloads == 0
    assert attachments.uploads == 0


@pytest.mark.asyncio
async def test_reserved_meta_events_are_not_forwarded_to_delegate(
    tmp_path: Path,
) -> None:
    delegate = ScriptedRuntime(
        events=[],
        result=UiPathRuntimeResult(status=UiPathRuntimeStatus.SUCCESSFUL),
    )
    runtime, _ = make_runtime(tmp_path, delegate, FakeAttachments())
    input = {
        "messages": [],
        "custom": "value",
        CONVERSATION_META_EVENTS_INPUT_KEY: [],
    }

    async for _ in runtime.stream(input):
        pass

    assert delegate.received_input == {"messages": [], "custom": "value"}
    assert input[CONVERSATION_META_EVENTS_INPUT_KEY] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("top_level", [False, True])
async def test_hydrates_single_workspace_snapshot(
    tmp_path: Path,
    top_level: bool,
) -> None:
    attachments = FakeAttachments()
    attachment_key = uuid.uuid4()
    attachments.files[attachment_key] = ("latest.txt", b"latest")
    delegate = ScriptedRuntime(
        events=[],
        result=UiPathRuntimeResult(status=UiPathRuntimeStatus.SUCCESSFUL),
    )
    runtime, workspace = make_runtime(tmp_path, delegate, attachments)
    input = {
        CONVERSATION_META_EVENTS_INPUT_KEY: [
            workspace_meta_event(
                [("nested/latest.txt", attachment_key)], top_level=top_level
            )
        ]
    }

    async for _ in runtime.stream(input):
        pass

    assert (workspace.path / "nested/latest.txt").read_bytes() == b"latest"
    assert attachments.downloads == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("top_level", [False, True])
async def test_hydrates_latest_workspace_snapshot(
    tmp_path: Path,
    top_level: bool,
) -> None:
    attachments = FakeAttachments()
    old_key = uuid.uuid4()
    latest_key = uuid.uuid4()
    attachments.files[old_key] = ("old.txt", b"old")
    attachments.files[latest_key] = ("latest.txt", b"latest")
    delegate = ScriptedRuntime(
        events=[],
        result=UiPathRuntimeResult(status=UiPathRuntimeStatus.SUCCESSFUL),
    )
    runtime, workspace = make_runtime(tmp_path, delegate, attachments)
    input = {
        "messages": [],
        CONVERSATION_META_EVENTS_INPUT_KEY: [
            workspace_meta_event([("old.txt", old_key)]),
            workspace_meta_event(
                [("nested/latest.txt", latest_key)], top_level=top_level
            ),
        ],
    }

    async for _ in runtime.stream(input):
        pass

    assert not (workspace.path / "old.txt").exists()
    assert (workspace.path / "nested/latest.txt").read_bytes() == b"latest"
    assert attachments.downloads == 1


@pytest.mark.asyncio
async def test_latest_empty_snapshot_does_not_restore_older_files(
    tmp_path: Path,
) -> None:
    attachments = FakeAttachments()
    old_key = uuid.uuid4()
    attachments.files[old_key] = ("old.txt", b"old")
    delegate = ScriptedRuntime(
        events=[],
        result=UiPathRuntimeResult(status=UiPathRuntimeStatus.SUCCESSFUL),
    )
    runtime, workspace = make_runtime(tmp_path, delegate, attachments)
    input = {
        CONVERSATION_META_EVENTS_INPUT_KEY: [
            workspace_meta_event([("old.txt", old_key)]),
            workspace_meta_event([]),
        ]
    }

    async for _ in runtime.stream(input):
        pass

    assert not (workspace.path / "old.txt").exists()
    assert attachments.downloads == 0


@pytest.mark.asyncio
async def test_persisted_empty_registry_wins_over_conversation_metadata(
    tmp_path: Path,
) -> None:
    attachments = FakeAttachments()
    attachment_key = uuid.uuid4()
    attachments.files[attachment_key] = ("old.txt", b"old")
    store = WorkspaceRegistryStore(MemoryStorage(), "runtime-1")
    await store.save({})
    delegate = ScriptedRuntime(
        events=[],
        result=UiPathRuntimeResult(status=UiPathRuntimeStatus.SUCCESSFUL),
    )
    runtime, workspace = make_runtime(tmp_path, delegate, attachments, store)

    async for _ in runtime.stream(
        {
            CONVERSATION_META_EVENTS_INPUT_KEY: [
                workspace_meta_event([("old.txt", attachment_key)])
            ]
        }
    ):
        pass

    assert not (workspace.path / "old.txt").exists()
    assert attachments.downloads == 0


@pytest.mark.asyncio
async def test_hydration_runtime_owns_persisted_registry_hydration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachments = FakeAttachments()
    store = WorkspaceRegistryStore(MemoryStorage(), "runtime-1")
    await store.save({})
    workspace = Workspace.create(tmp_path / "workspace")
    hydrator = WorkspaceHydrator(
        workspace_path=workspace.path,
        attachments=attachments,
    )
    hydrate_from_registry = AsyncMock(wraps=hydrator.hydrate_from_registry)
    monkeypatch.setattr(
        hydrator,
        "hydrate_from_registry",
        hydrate_from_registry,
    )
    delegate = ScriptedRuntime(
        events=[],
        result=UiPathRuntimeResult(status=UiPathRuntimeStatus.SUCCESSFUL),
    )
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

    async for _ in runtime.stream({}):
        pass

    hydrate_from_registry.assert_awaited_once_with({})


@pytest.mark.asyncio
async def test_faulted_result_does_not_emit_or_upload_snapshot(tmp_path: Path) -> None:
    attachments = FakeAttachments()
    delegate = ScriptedRuntime(
        events=[],
        result=UiPathRuntimeResult(status=UiPathRuntimeStatus.FAULTED),
        workspace_path=tmp_path / "workspace",
        files={"notes.txt": "hello"},
    )
    runtime, _ = make_runtime(tmp_path, delegate, attachments)

    events = [event async for event in runtime.stream({})]

    assert meta_events(events) == []
    assert attachments.uploads == 0
    assert isinstance(events[-1], UiPathRuntimeResult)


@pytest.mark.asyncio
async def test_execute_preserves_options(tmp_path: Path) -> None:
    delegate = ScriptedRuntime(
        events=[],
        result=UiPathRuntimeResult(status=UiPathRuntimeStatus.SUCCESSFUL),
    )
    runtime, _ = make_runtime(tmp_path, delegate, FakeAttachments())
    options = UiPathExecuteOptions(resume=True, breakpoints=["node-1"])

    await runtime.execute({}, options=options)

    assert delegate.received_options is not None
    assert delegate.received_options.resume is True
    assert delegate.received_options.breakpoints == ["node-1"]


@pytest.mark.asyncio
async def test_hydration_only_runs_once_across_resume_passes(tmp_path: Path) -> None:
    attachments = FakeAttachments()
    attachment_key = uuid.uuid4()
    attachments.files[attachment_key] = ("todo.md", b"step 1")
    delegate = ScriptedRuntime(
        events=[],
        result=UiPathRuntimeResult(status=UiPathRuntimeStatus.SUCCESSFUL),
    )
    runtime, _ = make_runtime(tmp_path, delegate, attachments)
    input = {
        CONVERSATION_META_EVENTS_INPUT_KEY: [
            workspace_meta_event([("todo.md", attachment_key)])
        ]
    }

    async for _ in runtime.stream(input):
        pass
    async for _ in runtime.stream({"interrupt-1": {"approved": True}}):
        pass

    assert attachments.downloads == 1


@pytest.mark.asyncio
async def test_failed_hydration_can_be_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachments = FakeAttachments()
    attachment_key = uuid.uuid4()
    attachments.files[attachment_key] = ("todo.md", b"step 1")
    delegate = ScriptedRuntime(
        events=[],
        result=UiPathRuntimeResult(status=UiPathRuntimeStatus.SUCCESSFUL),
    )
    runtime, workspace = make_runtime(tmp_path, delegate, attachments)
    input = {
        CONVERSATION_META_EVENTS_INPUT_KEY: [
            workspace_meta_event([("todo.md", attachment_key)])
        ]
    }
    download = attachments.download_async

    async def fail_download(**_: Any) -> str:
        raise RuntimeError("download failed")

    monkeypatch.setattr(attachments, "download_async", fail_download)
    with pytest.raises(RuntimeError, match="download failed"):
        async for _ in runtime.stream(input):
            pass

    monkeypatch.setattr(attachments, "download_async", download)
    async for _ in runtime.stream(input):
        pass

    assert (workspace.path / "todo.md").read_bytes() == b"step 1"


@pytest.mark.asyncio
async def test_attachment_upload_failure_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachments = FakeAttachments()
    delegate = ScriptedRuntime(
        events=[],
        result=UiPathRuntimeResult(status=UiPathRuntimeStatus.SUCCESSFUL),
        workspace_path=tmp_path / "workspace",
        files={"plan.md": "plan"},
    )
    runtime, _ = make_runtime(tmp_path, delegate, attachments)

    async def fail_upload(**_: Any) -> uuid.UUID:
        raise RuntimeError("upload failed")

    monkeypatch.setattr(attachments, "upload_async", fail_upload)

    with pytest.raises(RuntimeError, match="upload failed"):
        async for _ in runtime.stream({}):
            pass


@pytest.mark.asyncio
async def test_dispose_does_not_dispose_injected_dependencies(tmp_path: Path) -> None:
    delegate = ScriptedRuntime(
        events=[],
        result=UiPathRuntimeResult(status=UiPathRuntimeStatus.SUCCESSFUL),
    )
    workspace = Workspace.create(tmp_path / "workspace", cleanup=True)
    runtime = ConversationalWorkspaceRuntime(
        delegate,
        hydrator=WorkspaceHydrator(
            workspace_path=workspace.path,
            attachments=FakeAttachments(),
        ),
    )

    await runtime.dispose()

    assert not delegate.disposed
    assert workspace.path.exists()

    await delegate.dispose()
    await workspace.dispose()
