"""Attachment-backed workspace persistence for conversational agents."""

import logging
from collections.abc import Mapping
from typing import Any, AsyncGenerator

from uipath.runtime.base import (
    UiPathExecuteOptions,
    UiPathRuntimeProtocol,
    UiPathStreamOptions,
)
from uipath.runtime.events import (
    UiPathRuntimeConversationMetaEvent,
    UiPathRuntimeEvent,
)
from uipath.runtime.result import UiPathRuntimeResult, UiPathRuntimeStatus
from uipath.runtime.schema import UiPathRuntimeSchema
from uipath.runtime.workspace.hydrator import WorkspaceHydrator
from uipath.runtime.workspace.registry_store import WorkspaceRegistryStore

logger = logging.getLogger(__name__)

CONVERSATION_META_EVENTS_INPUT_KEY = "uipath__conversation_meta_events"
WORKSPACE_FILES_META_KEY = "workspaceFiles"
WORKSPACE_FILE_PATH_KEY = "path"
WORKSPACE_FILE_ATTACHMENT_KEY = "attachmentKey"


class _InvalidWorkspaceSnapshot(ValueError):
    pass


def _meta_event_payload(
    event: Mapping[object, object],
) -> Mapping[object, object] | None:
    exchange = event.get("exchange")
    if isinstance(exchange, Mapping):
        exchange_meta_event = exchange.get("metaEvent")
        if isinstance(exchange_meta_event, Mapping):
            return exchange_meta_event

    meta_event = event.get("metaEvent")
    return meta_event if isinstance(meta_event, Mapping) else None


def _attachment_keys_from_meta_events(
    input: Mapping[str, object] | None,
) -> dict[str, str] | None:
    events = (input or {}).get(CONVERSATION_META_EVENTS_INPUT_KEY)
    if not isinstance(events, list):
        return None

    for event in reversed(events):
        if not isinstance(event, Mapping):
            continue
        meta_event = _meta_event_payload(event)
        if meta_event is None or WORKSPACE_FILES_META_KEY not in meta_event:
            continue

        workspace_files = meta_event[WORKSPACE_FILES_META_KEY]
        if not isinstance(workspace_files, list):
            raise _InvalidWorkspaceSnapshot

        attachment_keys_by_path: dict[str, str] = {}
        for workspace_file in workspace_files:
            if not isinstance(workspace_file, Mapping):
                raise _InvalidWorkspaceSnapshot
            path = workspace_file.get(WORKSPACE_FILE_PATH_KEY)
            attachment_key = workspace_file.get(WORKSPACE_FILE_ATTACHMENT_KEY)
            if not isinstance(path, str) or not isinstance(attachment_key, str):
                raise _InvalidWorkspaceSnapshot
            attachment_keys_by_path[path] = attachment_key
        return attachment_keys_by_path

    return None


def _without_conversation_meta_events(
    input: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if input is None or CONVERSATION_META_EVENTS_INPUT_KEY not in input:
        return input
    return {
        key: value
        for key, value in input.items()
        if key != CONVERSATION_META_EVENTS_INPUT_KEY
    }


class ConversationalWorkspaceRuntime:
    """Persists workspace attachments between conversational jobs."""

    def __init__(
        self,
        delegate: UiPathRuntimeProtocol,
        *,
        hydrator: WorkspaceHydrator,
        registry_store: WorkspaceRegistryStore | None = None,
    ):
        """Initialize the wrapper with its delegate and hydrator."""
        self.delegate = delegate
        self.hydrator = hydrator
        self.registry_store = registry_store
        self._registry: dict[str, dict[str, Any]] = {}
        self._hydrated = False

    async def execute(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathExecuteOptions | None = None,
    ) -> UiPathRuntimeResult:
        """Execute by draining the stream."""
        result: UiPathRuntimeResult | None = None
        stream_options = (
            UiPathStreamOptions.model_validate(options.model_dump())
            if options is not None
            else None
        )
        async for event in self.stream(input, options=stream_options):
            if isinstance(event, UiPathRuntimeResult):
                result = event
        if result is None:
            raise RuntimeError("Delegate stream completed without a runtime result")
        return result

    async def stream(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathStreamOptions | None = None,
    ) -> AsyncGenerator[UiPathRuntimeEvent, None]:
        """Hydrate, stream the delegate, then emit the workspace snapshot."""
        await self._hydrate(input)
        delegate_input = _without_conversation_meta_events(input)
        final_result: UiPathRuntimeResult | None = None

        async for event in self.delegate.stream(delegate_input, options=options):
            if isinstance(event, UiPathRuntimeResult):
                final_result = event
            else:
                yield event

        if final_result is None:
            return
        if final_result.status == UiPathRuntimeStatus.SUCCESSFUL:
            yield await self._dehydrate()
        yield final_result

    async def get_schema(self) -> UiPathRuntimeSchema:
        """Passthrough schema from delegate runtime."""
        return await self.delegate.get_schema()

    async def dispose(self) -> None:
        """Release resources owned by this wrapper."""

    async def _hydrate(self, input: Mapping[str, object] | None) -> None:
        if self._hydrated:
            return

        persisted_registry = (
            await self.registry_store.try_load() if self.registry_store else None
        )
        if persisted_registry is not None:
            self._registry = persisted_registry
            source = "suspended job state"
        else:
            try:
                attachment_keys_by_path = _attachment_keys_from_meta_events(input)
            except _InvalidWorkspaceSnapshot:
                logger.warning("Ignoring malformed conversational workspace snapshot")
                source = "existing workspace"
            else:
                if attachment_keys_by_path is None:
                    source = "existing workspace"
                else:
                    source = "conversation metadata"
                    self._registry = await self.hydrator.hydrate_from_attachments(
                        attachment_keys_by_path
                    )

        logger.info(
            "Conversational workspace initialized: %d file(s) from %s",
            len(self._registry),
            source,
        )
        self._hydrated = True

    async def _dehydrate(self) -> UiPathRuntimeConversationMetaEvent:
        registry = self._registry
        if self.registry_store is not None:
            persisted_registry = await self.registry_store.try_load()
            if persisted_registry is not None:
                registry = persisted_registry

        self._registry = await self.hydrator.dehydrate(registry)
        if self.registry_store is not None:
            await self.registry_store.save(self._registry)

        workspace_files = [
            {
                WORKSPACE_FILE_PATH_KEY: virtual_path,
                WORKSPACE_FILE_ATTACHMENT_KEY: entry["attachment_key"],
            }
            for virtual_path, entry in sorted(self._registry.items())
        ]
        logger.info(
            "Conversational workspace dehydrate: emitting %d file(s)",
            len(workspace_files),
        )
        return UiPathRuntimeConversationMetaEvent(
            payload={WORKSPACE_FILES_META_KEY: workspace_files}
        )
