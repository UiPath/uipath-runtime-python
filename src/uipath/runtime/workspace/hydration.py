"""Runtime wrapper for workspace hydration."""

from enum import Enum
from typing import Any, AsyncGenerator

from uipath.runtime.base import (
    UiPathExecuteOptions,
    UiPathRuntimeProtocol,
    UiPathStreamOptions,
)
from uipath.runtime.events import UiPathRuntimeEvent
from uipath.runtime.result import UiPathRuntimeResult, UiPathRuntimeStatus
from uipath.runtime.schema import UiPathRuntimeSchema
from uipath.runtime.workspace.hydrator import WorkspaceHydrator
from uipath.runtime.workspace.registry_store import WorkspaceRegistryStore
from uipath.runtime.workspace.workspace import Workspace


class HydrationPolicy(str, Enum):
    """Controls when workspace changes are persisted."""

    SUSPEND_ONLY = "suspend_only"
    SUSPEND_OR_SUCCESS = "suspend_or_success"
    ALWAYS = "always"


class HydrationRuntime:
    """Wraps a runtime with hydrate-before and dehydrate-after behavior."""

    def __init__(
        self,
        delegate: UiPathRuntimeProtocol,
        *,
        workspace: Workspace,
        hydrator: WorkspaceHydrator,
        registry_store: WorkspaceRegistryStore,
        policy: HydrationPolicy = HydrationPolicy.SUSPEND_ONLY,
    ):
        """Initialize the hydration wrapper."""
        self.delegate = delegate
        self.workspace = workspace
        self.hydrator = hydrator
        self.registry_store = registry_store
        self.policy = policy

    async def execute(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathExecuteOptions | None = None,
    ) -> UiPathRuntimeResult:
        """Hydrate, execute, then persist files according to policy."""
        await self._hydrate()
        try:
            result = await self.delegate.execute(input, options=options)
        except Exception:
            if self.policy == HydrationPolicy.ALWAYS:
                await self._persist()
            raise
        await self._dehydrate(result)
        return result

    async def stream(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathStreamOptions | None = None,
    ) -> AsyncGenerator[UiPathRuntimeEvent, None]:
        """Hydrate, stream delegate events, then persist files according to policy."""
        await self._hydrate()
        final_result: UiPathRuntimeResult | None = None

        try:
            async for event in self.delegate.stream(input, options=options):
                if isinstance(event, UiPathRuntimeResult):
                    final_result = event
                else:
                    yield event
        except Exception:
            if self.policy == HydrationPolicy.ALWAYS:
                await self._persist()
            raise

        if final_result is not None:
            await self._dehydrate(final_result)
            yield final_result

    async def get_schema(self) -> UiPathRuntimeSchema:
        """Passthrough schema from delegate runtime."""
        return await self.delegate.get_schema()

    async def dispose(self) -> None:
        """Dispose delegate and workspace."""
        try:
            await self.delegate.dispose()
        finally:
            await self.workspace.dispose()

    async def _hydrate(self) -> None:
        registry = await self.registry_store.load()
        hydrated = await self.hydrator.hydrate(registry)
        if hydrated != registry:
            await self.registry_store.save(hydrated)

    async def _dehydrate(self, result: UiPathRuntimeResult) -> None:
        if self._should_dehydrate(result):
            await self._persist()

    async def _persist(self) -> None:
        registry = await self.registry_store.load()
        dehydrated = await self.hydrator.dehydrate(registry)
        await self.registry_store.save(dehydrated)

    def _should_dehydrate(self, result: UiPathRuntimeResult) -> bool:
        if self.policy == HydrationPolicy.ALWAYS:
            return True
        if result.status == UiPathRuntimeStatus.SUSPENDED:
            return True
        return (
            self.policy == HydrationPolicy.SUSPEND_OR_SUCCESS
            and result.status == UiPathRuntimeStatus.SUCCESSFUL
        )
