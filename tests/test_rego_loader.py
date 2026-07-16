"""Tests for the Rego async evaluator bootstrap (loader.py)."""
from __future__ import annotations

import io
import tarfile
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from uipath.core.governance.models import LifecycleHook


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tar_gz(files: dict[str, bytes]) -> bytes:
    """Build an in-memory .tar.gz with the given filename → bytes mapping."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _fake_bundle(wasm: bytes = b"\x00asm\x01\x00\x00\x00", data: dict[str, object] | None = None) -> bytes:
    """Build a minimal OPA .tar.gz bundle."""
    import json
    files: dict[str, bytes] = {"policy.wasm": wasm}
    if data is not None:
        files["data.json"] = json.dumps(data).encode()
    return _make_tar_gz(files)


def _hook_bundle(hook_type: str, etag: str = "etag-1") -> Any:
    b = MagicMock()
    b.hook_type = hook_type
    b.bundle_url = f"https://cdn.example.com/{hook_type}.tar.gz"
    b.etag = etag
    return b


def _all_policies_response(*hook_bundles: Any) -> Any:
    r = MagicMock()
    r.hook_bundles = list(hook_bundles)
    return r


# ---------------------------------------------------------------------------
# build_rego_evaluator_async — no bundles
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_returns_none_when_no_hook_bundles() -> None:
    from uipath.runtime.governance.rego.loader import build_rego_evaluator_async

    service = MagicMock()
    service.retrieve_all_policies_async = AsyncMock(return_value=_all_policies_response())

    with patch("uipath.runtime.governance.native.backend_client.resolve_organization_id", return_value="org1"), \
         patch("uipath.runtime.governance.native.backend_client.resolve_tenant_id", return_value="tenant1"):
        result = await build_rego_evaluator_async(service)

    assert result is None
    service.download_bundle_async.assert_not_called()


# ---------------------------------------------------------------------------
# build_rego_evaluator_async — missing org/tenant
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_returns_none_when_org_id_missing() -> None:
    from uipath.runtime.governance.rego.loader import build_rego_evaluator_async

    service = MagicMock()
    with patch("uipath.runtime.governance.native.backend_client.resolve_organization_id", return_value=None), \
         patch("uipath.runtime.governance.native.backend_client.resolve_tenant_id", return_value="tenant1"):
        result = await build_rego_evaluator_async(service)

    assert result is None


@pytest.mark.asyncio
async def test_returns_none_when_tenant_id_missing() -> None:
    from uipath.runtime.governance.rego.loader import build_rego_evaluator_async

    service = MagicMock()
    with patch("uipath.runtime.governance.native.backend_client.resolve_organization_id", return_value="org1"), \
         patch("uipath.runtime.governance.native.backend_client.resolve_tenant_id", return_value=None):
        result = await build_rego_evaluator_async(service)

    assert result is None


# ---------------------------------------------------------------------------
# build_rego_evaluator_async — service raises
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_returns_none_when_service_raises() -> None:
    from uipath.runtime.governance.rego.loader import build_rego_evaluator_async

    service = MagicMock()
    service.retrieve_all_policies_async = AsyncMock(side_effect=RuntimeError("network error"))

    with patch("uipath.runtime.governance.native.backend_client.resolve_organization_id", return_value="org1"), \
         patch("uipath.runtime.governance.native.backend_client.resolve_tenant_id", return_value="tenant1"):
        result = await build_rego_evaluator_async(service)

    assert result is None


# ---------------------------------------------------------------------------
# build_rego_evaluator_async — unknown hook type skipped
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unknown_hook_type_is_skipped(tmp_path: Path) -> None:
    from uipath.runtime.governance.rego.loader import build_rego_evaluator_async

    bundle_data = _fake_bundle()
    service = MagicMock()
    service.retrieve_all_policies_async = AsyncMock(
        return_value=_all_policies_response(_hook_bundle("unknown_hook_xyz"))
    )
    service.download_bundle_async = AsyncMock(return_value=bundle_data)

    with patch("uipath.runtime.governance.native.backend_client.resolve_organization_id", return_value="org1"), \
         patch("uipath.runtime.governance.native.backend_client.resolve_tenant_id", return_value="tenant1"), \
         patch("uipath.runtime.governance.rego.loader.get_cache_dir", return_value=tmp_path), \
         patch("uipath.runtime.governance.rego.loader.get_cached_etag", return_value=None), \
         patch("uipath.runtime.governance.rego.loader.get_cached_bundle_path", return_value=tmp_path / "b.tar.gz"), \
         patch("uipath.runtime.governance.rego.loader.save_bundle"):
        result = await build_rego_evaluator_async(service)

    assert result is None


# ---------------------------------------------------------------------------
# build_rego_evaluator_async — etag cache hit skips download
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_etag_cache_hit_skips_download(tmp_path: Path) -> None:
    from uipath.runtime.governance.rego.loader import build_rego_evaluator_async

    bundle_path = tmp_path / "bundle.tar.gz"
    bundle_path.write_bytes(_fake_bundle())

    service = MagicMock()
    service.retrieve_all_policies_async = AsyncMock(
        return_value=_all_policies_response(_hook_bundle("before_model", etag="etag-cached"))
    )
    service.download_bundle_async = AsyncMock()

    mock_evaluator = MagicMock()
    mock_evaluator.loaded_hooks = [LifecycleHook.BEFORE_MODEL]

    with patch("uipath.runtime.governance.native.backend_client.resolve_organization_id", return_value="org1"), \
         patch("uipath.runtime.governance.native.backend_client.resolve_tenant_id", return_value="tenant1"), \
         patch("uipath.runtime.governance.rego.loader.get_cache_dir", return_value=tmp_path), \
         patch("uipath.runtime.governance.rego.loader.get_cached_etag", return_value="etag-cached"), \
         patch("uipath.runtime.governance.rego.loader.get_cached_bundle_path", return_value=bundle_path), \
         patch("uipath.runtime.governance.rego.evaluator._extract_data_json_from_bundle", return_value=None), \
         patch("uipath.runtime.governance.rego.evaluator.RegoEvaluator", return_value=mock_evaluator):
        result = await build_rego_evaluator_async(service)

    service.download_bundle_async.assert_not_called()
    assert result is mock_evaluator


# ---------------------------------------------------------------------------
# build_rego_evaluator_async — etag mismatch triggers download
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_etag_mismatch_triggers_download(tmp_path: Path) -> None:
    from uipath.runtime.governance.rego.loader import build_rego_evaluator_async

    bundle_data = _fake_bundle()
    bundle_path = tmp_path / "bundle.tar.gz"
    bundle_path.write_bytes(bundle_data)

    service = MagicMock()
    service.retrieve_all_policies_async = AsyncMock(
        return_value=_all_policies_response(_hook_bundle("before_model", etag="etag-new"))
    )
    service.download_bundle_async = AsyncMock(return_value=bundle_data)

    mock_evaluator = MagicMock()

    with patch("uipath.runtime.governance.native.backend_client.resolve_organization_id", return_value="org1"), \
         patch("uipath.runtime.governance.native.backend_client.resolve_tenant_id", return_value="tenant1"), \
         patch("uipath.runtime.governance.rego.loader.get_cache_dir", return_value=tmp_path), \
         patch("uipath.runtime.governance.rego.loader.get_cached_etag", return_value="etag-old"), \
         patch("uipath.runtime.governance.rego.loader.get_cached_bundle_path", return_value=bundle_path), \
         patch("uipath.runtime.governance.rego.loader.save_bundle"), \
         patch("uipath.runtime.governance.rego.evaluator._extract_data_json_from_bundle", return_value=None), \
         patch("uipath.runtime.governance.rego.evaluator.RegoEvaluator", return_value=mock_evaluator):
        result = await build_rego_evaluator_async(service)

    service.download_bundle_async.assert_called_once()
    assert result is mock_evaluator
