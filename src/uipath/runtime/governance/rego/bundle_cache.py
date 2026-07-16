"""Disk cache for per-hook Rego WASM bundles.

Layout under ``$TMPDIR/uipath-governance/{key}/hooks/{hook_type}/``:
  - ``bundle.tar.gz``  — raw OPA bundle bytes
  - ``etag.txt``       — the ETag string from the last successful download
"""
from __future__ import annotations

import hashlib
import logging
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

_BUNDLE_FILE = "bundle.tar.gz"
_ETAG_FILE = "etag.txt"


def get_cache_dir(org_id: str, tenant_id: str) -> Path:
    """Return (and create) the cache root for the given org/tenant pair."""
    key = hashlib.sha256(f"{org_id}{tenant_id}".encode()).hexdigest()[:16]
    base = Path(tempfile.gettempdir()) / "uipath-governance" / key
    base.mkdir(parents=True, exist_ok=True)
    return base


def _hook_dir(cache_dir: Path, hook_type: str) -> Path:
    return cache_dir / "hooks" / hook_type


def get_cached_etag(cache_dir: Path, hook_type: str) -> str | None:
    """Return the cached ETag for ``hook_type``, or ``None`` if absent."""
    etag_path = _hook_dir(cache_dir, hook_type) / _ETAG_FILE
    try:
        return etag_path.read_text(encoding="utf-8").strip() or None
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("Failed to read cached ETag for %s: %s", hook_type, exc)
        return None


def get_cached_bundle_path(cache_dir: Path, hook_type: str) -> Path | None:
    """Return the cached bundle path for ``hook_type``, or ``None`` if absent."""
    bundle_path = _hook_dir(cache_dir, hook_type) / _BUNDLE_FILE
    return bundle_path if bundle_path.exists() else None


def save_bundle(cache_dir: Path, hook_type: str, data: bytes, etag: str) -> Path:
    """Write ``data`` and ``etag`` to the cache for ``hook_type``.

    Returns the path to the written bundle file.
    """
    hook_dir = _hook_dir(cache_dir, hook_type)
    hook_dir.mkdir(parents=True, exist_ok=True)

    bundle_path = hook_dir / _BUNDLE_FILE
    bundle_path.write_bytes(data)

    etag_path = hook_dir / _ETAG_FILE
    etag_path.write_text(etag, encoding="utf-8")

    logger.debug("Cached bundle for hook=%s (%d bytes, etag=%s)", hook_type, len(data), etag)
    return bundle_path
