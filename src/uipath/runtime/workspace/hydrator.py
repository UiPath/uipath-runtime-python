"""Attachment-backed workspace hydration."""

import hashlib
import os
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel
from uipath.core.workspace import AttachmentsProtocol, JobsProtocol


class AttachmentRegistryEntry(BaseModel):
    """Registry entry for one workspace file attachment."""

    attachment_key: str
    sha256: str
    size: int
    uploaded_at: str
    attachment_name: str | None = None


class WorkspaceHydrator:
    """Hydrates/dehydrates a workspace directory through job attachments."""

    DEFAULT_ATTACHMENT_PREFIX = ".uipath-workspace/"

    def __init__(
        self,
        *,
        workspace_path: str | Path,
        attachments: AttachmentsProtocol,
        jobs: JobsProtocol | None = None,
        current_job_key: str | None = None,
        folder_key: str | None = None,
        folder_path: str | None = None,
        attachment_prefix: str = DEFAULT_ATTACHMENT_PREFIX,
    ):
        """Initialize the hydrator."""
        self.workspace_path = Path(workspace_path)
        self.attachments = attachments
        self.jobs = jobs
        self.current_job_key = current_job_key
        self.folder_key = folder_key
        self.folder_path = folder_path
        self.attachment_prefix = attachment_prefix.strip("/")

    async def hydrate(
        self,
        registry: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        """Restore a registry using the original public API."""
        return await self.hydrate_from_registry(registry)

    async def hydrate_from_registry(
        self,
        registry: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        """Restore registry attachments into the workspace.

        Files with matching SHA-256 are left untouched.
        """
        normalized = self._normalize_registry(registry)
        for virtual_path, entry in normalized.items():
            target = self._resolve_workspace_path(virtual_path)
            if target.exists() and self._sha256(target) == entry.sha256:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            await self.attachments.download_async(
                key=UUID(entry.attachment_key),
                destination_path=str(target),
                folder_key=self.folder_key,
                folder_path=self.folder_path,
            )
        return self._dump_registry(normalized)

    async def hydrate_from_attachments(
        self,
        attachment_keys_by_path: dict[str, str],
    ) -> dict[str, dict[str, Any]]:
        """Replace the workspace with an attachment snapshot."""
        attachments_by_path: dict[str, UUID] = {}
        for virtual_path, raw_attachment_key in attachment_keys_by_path.items():
            normalized_path = self._virtual_path(
                self._resolve_workspace_path(virtual_path)
            )
            attachments_by_path[normalized_path] = UUID(raw_attachment_key)

        registry: dict[str, AttachmentRegistryEntry] = {}
        for virtual_path, parsed_attachment_key in attachments_by_path.items():
            target = self._resolve_workspace_path(virtual_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            await self.attachments.download_async(
                key=parsed_attachment_key,
                destination_path=str(target),
                folder_key=self.folder_key,
                folder_path=self.folder_path,
            )
            registry[virtual_path] = AttachmentRegistryEntry(
                attachment_key=str(parsed_attachment_key),
                sha256=self._sha256(target),
                size=target.stat().st_size,
                uploaded_at=datetime.now(timezone.utc).isoformat(),
                attachment_name=self._attachment_name_for_virtual_path(virtual_path),
            )
        self._remove_files_not_in(set(registry))
        return self._dump_registry(registry)

    async def dehydrate(
        self,
        registry: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        """Upload new/changed workspace files and return the merged registry.

        The result is rebuilt from the files currently present, so entries for
        files deleted locally are dropped and not restored on the next hydrate.
        """
        previous = self._normalize_registry(registry)
        current: dict[str, AttachmentRegistryEntry] = {}
        for file_path in self._iter_files():
            virtual_path = self._virtual_path(file_path)
            digest = self._sha256(file_path)
            size = file_path.stat().st_size
            existing = previous.get(virtual_path)

            if existing and existing.sha256 == digest and existing.size == size:
                current[virtual_path] = existing
                continue

            attachment_name = self._attachment_name_for_virtual_path(virtual_path)
            attachment_key = await self.attachments.upload_async(
                name=attachment_name,
                source_path=str(file_path),
                folder_key=self.folder_key,
                folder_path=self.folder_path,
            )
            entry = AttachmentRegistryEntry(
                attachment_key=str(attachment_key),
                sha256=digest,
                size=size,
                uploaded_at=datetime.now(timezone.utc).isoformat(),
                attachment_name=attachment_name,
            )
            current[virtual_path] = entry

        await self._link_attachments(entry.attachment_key for entry in current.values())

        return self._dump_registry(current)

    async def link_attachment(self, attachment_key: str) -> None:
        """Link an attachment unless it is already associated with the current job."""
        await self._link_attachments((attachment_key,))

    async def _link_attachments(self, attachment_keys: Iterable[str]) -> None:
        if self.jobs is None or not self.current_job_key:
            return

        requested_attachment_keys = list(
            dict.fromkeys(UUID(key) for key in attachment_keys)
        )
        if not requested_attachment_keys:
            return

        job_key = UUID(self.current_job_key)
        linked_attachment_keys = {
            UUID(key)
            for key in await self.jobs.list_attachments_async(
                job_key=job_key,
                folder_key=self.folder_key,
                folder_path=self.folder_path,
            )
        }
        for attachment_key in requested_attachment_keys:
            if attachment_key in linked_attachment_keys:
                continue

            await self.jobs.link_attachment_async(
                job_key=job_key,
                attachment_key=attachment_key,
                folder_key=self.folder_key,
                folder_path=self.folder_path,
            )
            linked_attachment_keys.add(attachment_key)

    def _iter_files(self) -> list[Path]:
        if not self.workspace_path.exists():
            return []

        files: list[Path] = []
        for root, _, names in os.walk(self.workspace_path):
            for name in names:
                path = Path(root) / name
                if path.is_file():
                    files.append(path)
        return sorted(files)

    def _resolve_workspace_path(self, virtual_path: str) -> Path:
        path = (self.workspace_path / virtual_path).resolve()
        workspace = self.workspace_path.resolve()
        if workspace != path and workspace not in path.parents:
            raise ValueError(f"Workspace path escapes root: {virtual_path}")
        return path

    def _virtual_path(self, path: Path) -> str:
        return path.relative_to(self.workspace_path).as_posix()

    def _remove_files_not_in(self, virtual_paths: set[str]) -> None:
        for path in self._iter_files():
            if self._virtual_path(path) not in virtual_paths:
                path.unlink()

    @staticmethod
    def _escape(value: str) -> str:
        # json-pointer escaping; escape ~ before / so they can't collide
        return value.replace("~", "~0").replace("/", "~1")

    @staticmethod
    def _unescape(value: str) -> str:
        return value.replace("~1", "/").replace("~0", "~")

    def _attachment_name_for_virtual_path(self, virtual_path: str) -> str:
        # the platform mishandles "/" in attachment names, so keep it slash-free
        return self._escape(f"{self.attachment_prefix}/{virtual_path}")

    def _virtual_path_from_attachment_name(self, name: str) -> str | None:
        decoded = self._unescape(name)
        prefix = f"{self.attachment_prefix}/"
        if not decoded.startswith(prefix):
            return None
        virtual_path = decoded[len(prefix) :]
        self._resolve_workspace_path(virtual_path)
        return virtual_path

    def _normalize_registry(
        self, registry: dict[str, dict[str, Any]]
    ) -> dict[str, AttachmentRegistryEntry]:
        normalized: dict[str, AttachmentRegistryEntry] = {}
        for virtual_path, entry in registry.items():
            normalized[virtual_path] = AttachmentRegistryEntry.model_validate(entry)
        return normalized

    def _dump_registry(
        self, registry: dict[str, AttachmentRegistryEntry]
    ) -> dict[str, dict[str, Any]]:
        return {path: entry.model_dump() for path, entry in registry.items()}

    def _sha256(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
