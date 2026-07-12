"""Tests for the Rego WASM bundle disk cache."""
from __future__ import annotations

from pathlib import Path

import pytest

from uipath.runtime.governance.rego.bundle_cache import (
    get_cache_dir,
    get_cached_bundle_path,
    get_cached_etag,
    save_bundle,
)


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    return get_cache_dir.__wrapped__(tmp_path) if hasattr(get_cache_dir, "__wrapped__") else tmp_path / "cache"


# ---------------------------------------------------------------------------
# get_cache_dir
# ---------------------------------------------------------------------------

def test_get_cache_dir_creates_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import tempfile
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    result = get_cache_dir("org1", "tenant1")
    assert result.exists()
    assert result.is_dir()


def test_get_cache_dir_same_org_tenant_returns_same_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import tempfile
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    a = get_cache_dir("org1", "tenant1")
    b = get_cache_dir("org1", "tenant1")
    assert a == b


def test_get_cache_dir_different_pairs_return_different_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import tempfile
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    a = get_cache_dir("org1", "tenant1")
    b = get_cache_dir("org1", "tenant2")
    assert a != b


# ---------------------------------------------------------------------------
# save_bundle / get_cached_bundle_path / get_cached_etag
# ---------------------------------------------------------------------------

def test_save_and_retrieve_bundle(tmp_path: Path) -> None:
    bundle_path = save_bundle(tmp_path, "before_model", b"fake-wasm", "etag-abc")
    assert bundle_path.exists()
    assert bundle_path.read_bytes() == b"fake-wasm"


def test_get_cached_bundle_path_returns_path_after_save(tmp_path: Path) -> None:
    save_bundle(tmp_path, "before_model", b"wasm-data", "etag-1")
    result = get_cached_bundle_path(tmp_path, "before_model")
    assert result is not None
    assert result.exists()


def test_get_cached_bundle_path_returns_none_when_missing(tmp_path: Path) -> None:
    result = get_cached_bundle_path(tmp_path, "before_model")
    assert result is None


def test_get_cached_etag_returns_etag_after_save(tmp_path: Path) -> None:
    save_bundle(tmp_path, "after_agent", b"wasm", "etag-xyz")
    result = get_cached_etag(tmp_path, "after_agent")
    assert result == "etag-xyz"


def test_get_cached_etag_returns_none_when_missing(tmp_path: Path) -> None:
    result = get_cached_etag(tmp_path, "after_agent")
    assert result is None


def test_save_bundle_overwrites_existing(tmp_path: Path) -> None:
    save_bundle(tmp_path, "before_model", b"old", "etag-old")
    save_bundle(tmp_path, "before_model", b"new", "etag-new")
    path = get_cached_bundle_path(tmp_path, "before_model")
    assert path is not None
    assert path.read_bytes() == b"new"
    assert get_cached_etag(tmp_path, "before_model") == "etag-new"


def test_different_hook_types_are_isolated(tmp_path: Path) -> None:
    save_bundle(tmp_path, "before_model", b"wasm-bm", "etag-bm")
    save_bundle(tmp_path, "after_agent", b"wasm-aa", "etag-aa")
    assert get_cached_etag(tmp_path, "before_model") == "etag-bm"
    assert get_cached_etag(tmp_path, "after_agent") == "etag-aa"
    assert get_cached_bundle_path(tmp_path, "tool_call") is None


def test_get_cached_etag_handles_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    save_bundle(tmp_path, "before_model", b"wasm", "etag-1")
    # Make the etag file unreadable
    hook_dir = tmp_path / "hooks" / "before_model"
    etag_file = hook_dir / "etag.txt"
    etag_file.chmod(0o000)
    try:
        result = get_cached_etag(tmp_path, "before_model")
        assert result is None
    finally:
        etag_file.chmod(0o644)
