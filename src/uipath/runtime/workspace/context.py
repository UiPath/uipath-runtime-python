"""Execution-scoped access to a managed workspace."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from uipath.runtime.errors import (
    UiPathErrorCategory,
    UiPathErrorCode,
    UiPathRuntimeError,
)

_workspace_path: ContextVar[Path | None] = ContextVar(
    "uipath_workspace_path", default=None
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
    workspace_path = _workspace_path.get()
    if workspace_path is None:
        raise UiPathRuntimeError(
            code=UiPathErrorCode.MANAGED_WORKSPACE_UNAVAILABLE,
            title="Managed Workspace Unavailable",
            detail=(
                "No managed workspace is available outside a runtime execution. "
                "Call get_workspace_path() only from a graph node or tool."
            ),
            category=UiPathErrorCategory.USER,
            include_traceback=False,
        )
    return workspace_path


@contextmanager
def _workspace_execution(path: Path) -> Iterator[None]:
    """Make ``path`` available for one managed runtime execution."""
    token = _workspace_path.set(path)
    try:
        yield
    finally:
        _workspace_path.reset(token)
