"""Disk workspace lifecycle helpers."""

import shutil
import tempfile
from pathlib import Path


class Workspace:
    """Owns a directory used by a runtime to persist files between executions."""

    def __init__(self, path: Path, *, cleanup: bool = True):
        """Initialize the workspace.

        Args:
            path: Workspace directory.
            cleanup: Whether dispose should remove the directory.
        """
        self.path = path
        self.cleanup = cleanup

    @classmethod
    def create(
        cls,
        path: str | Path | None = None,
        *,
        prefix: str = "uipath-workspace-",
        cleanup: bool | None = None,
    ) -> "Workspace":
        """Create a workspace directory.

        ``cleanup`` defaults to True for an owned temp dir and False for a
        supplied path; pass ``cleanup=True`` for a temp path you want disposed.
        """
        if path is None:
            workspace_path = Path(tempfile.mkdtemp(prefix=prefix))
            should_cleanup = True if cleanup is None else cleanup
        else:
            workspace_path = Path(path)
            workspace_path.mkdir(parents=True, exist_ok=True)
            should_cleanup = False if cleanup is None else cleanup

        return cls(workspace_path, cleanup=should_cleanup)

    async def dispose(self) -> None:
        """Remove the workspace directory when configured to do so."""
        if self.cleanup and self.path.exists():
            shutil.rmtree(self.path)
