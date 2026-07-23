"""Execution-scoped access to a managed workspace."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from uipath.runtime.errors import (
    UiPathErrorCategory,
    UiPathErrorCode,
    UiPathRuntimeError,
)


@dataclass
class _WorkspaceExecution:
    """Revocable workspace state shared with tasks created during execution."""

    path: Path
    active: bool = True


_workspace_execution_state: ContextVar[_WorkspaceExecution | None] = ContextVar(
    "uipath_workspace_execution", default=None
)


def get_workspace_path() -> Path:
    """Return the workspace available to the current runtime execution.

    A workspace is available only while a runtime managed by
    :class:`HydrationRuntime` is executing. Files created beneath this path are
    restored before a resumed execution and persisted when the runtime
    suspends, according to its hydration policy.

    Raises:
        UiPathRuntimeError: If called outside a managed runtime execution.
    """
    execution = _workspace_execution_state.get()
    if execution is None or not execution.active:
        raise UiPathRuntimeError(
            code=UiPathErrorCode.MANAGED_WORKSPACE_UNAVAILABLE,
            title="Managed Workspace Unavailable",
            detail=(
                "No managed workspace is available in the current code path. "
                "Call get_workspace_path() only from code invoked by a managed runtime, "
                "not from module-level initialization or from the caller that invokes "
                "or consumes the execution."
            ),
            category=UiPathErrorCategory.USER,
            include_traceback=False,
        )
    return execution.path


@contextmanager
def _bind_workspace_execution(execution: _WorkspaceExecution) -> Iterator[None]:
    """Make an active execution available in the current context."""
    token = _workspace_execution_state.set(execution)
    try:
        yield
    finally:
        _workspace_execution_state.reset(token)


def _create_workspace_execution(path: Path) -> _WorkspaceExecution:
    """Create state for a workspace execution."""
    return _WorkspaceExecution(path)


def _revoke_workspace_execution(execution: _WorkspaceExecution) -> None:
    """Prevent all contexts that inherited ``execution`` from accessing it."""
    execution.active = False


@contextmanager
def _workspace_execution(path: Path) -> Iterator[None]:
    """Make ``path`` available for one non-streaming execution."""
    execution = _create_workspace_execution(path)
    try:
        with _bind_workspace_execution(execution):
            yield
    finally:
        _revoke_workspace_execution(execution)
