"""Tests for ``fetch_policy_response`` and the body parser.

Covers the skip paths (missing org / tenant / token), HTTP failures
(HTTPError, URLError, TimeoutError, OSError), and body parsing
(empty body, non-UTF8, malformed JSON, wrong top-level shape, bad
``policies`` type).
"""

from __future__ import annotations

import io
import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from uipath.runtime.governance.native import policy_api_client
from uipath.runtime.governance.native.policy_api_client import (
    PolicyResponse,
    _parse_policy_body,
    build_policy_url,
    fetch_policy_response,
)


@pytest.fixture
def _fresh_env(monkeypatch: pytest.MonkeyPatch):
    """Clear the env vars that the fetch path depends on."""
    for var in (
        "UIPATH_ORGANIZATION_ID",
        "UIPATH_TENANT_ID",
        "UIPATH_ACCESS_TOKEN",
        "UIPATH_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


@pytest.fixture
def _populated_env(monkeypatch: pytest.MonkeyPatch):
    """All three vars present — the fetch path can reach urlopen."""
    monkeypatch.setenv("UIPATH_ORGANIZATION_ID", "org-1")
    monkeypatch.setenv("UIPATH_TENANT_ID", "tenant-1")
    monkeypatch.setenv("UIPATH_ACCESS_TOKEN", "tok-abc")
    monkeypatch.setenv("UIPATH_URL", "https://alpha.uipath.com")
    yield


def _ok_response(body: bytes) -> MagicMock:
    """urlopen()-compatible context manager that returns ``body``."""
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


# ---------------------------------------------------------------------------
# Skip paths — fail-open without contacting the backend
# ---------------------------------------------------------------------------


def test_skip_when_org_id_missing(_fresh_env, monkeypatch) -> None:
    monkeypatch.setenv("UIPATH_TENANT_ID", "tenant-1")
    monkeypatch.setenv("UIPATH_ACCESS_TOKEN", "tok")
    with patch.object(
        policy_api_client.urllib.request, "urlopen"
    ) as mock_urlopen:
        assert fetch_policy_response() is None
    assert not mock_urlopen.called


def test_skip_when_tenant_id_missing(_fresh_env, monkeypatch) -> None:
    monkeypatch.setenv("UIPATH_ORGANIZATION_ID", "org-1")
    monkeypatch.setenv("UIPATH_ACCESS_TOKEN", "tok")
    with patch.object(
        policy_api_client.urllib.request, "urlopen"
    ) as mock_urlopen:
        assert fetch_policy_response() is None
    assert not mock_urlopen.called


def test_skip_when_token_missing(_fresh_env, monkeypatch) -> None:
    monkeypatch.setenv("UIPATH_ORGANIZATION_ID", "org-1")
    monkeypatch.setenv("UIPATH_TENANT_ID", "tenant-1")
    with patch.object(
        policy_api_client.urllib.request, "urlopen"
    ) as mock_urlopen:
        assert fetch_policy_response() is None
    assert not mock_urlopen.called


# ---------------------------------------------------------------------------
# HTTP failure paths — fail-open with a warning
# ---------------------------------------------------------------------------


def test_returns_none_on_http_error(_populated_env) -> None:
    err = urllib.error.HTTPError(
        url="x", code=500, msg="Server Error", hdrs=None, fp=io.BytesIO(b"")
    )
    with patch.object(
        policy_api_client.urllib.request, "urlopen", side_effect=err
    ):
        assert fetch_policy_response() is None


def test_returns_none_on_url_error(_populated_env) -> None:
    err = urllib.error.URLError("connection refused")
    with patch.object(
        policy_api_client.urllib.request, "urlopen", side_effect=err
    ):
        assert fetch_policy_response() is None


def test_returns_none_on_timeout(_populated_env) -> None:
    with patch.object(
        policy_api_client.urllib.request, "urlopen", side_effect=TimeoutError()
    ):
        assert fetch_policy_response() is None


def test_returns_none_on_os_error(_populated_env) -> None:
    with patch.object(
        policy_api_client.urllib.request,
        "urlopen",
        side_effect=OSError("disk full"),
    ):
        assert fetch_policy_response() is None


def test_outer_swallows_unexpected_exception(_populated_env) -> None:
    """Even non-HTTP exceptions from urlopen don't escape the fetch helper."""
    with patch.object(
        policy_api_client.urllib.request,
        "urlopen",
        side_effect=RuntimeError("library bug"),
    ):
        assert fetch_policy_response() is None


# ---------------------------------------------------------------------------
# Headers / URL composition
# ---------------------------------------------------------------------------


def test_sends_no_content_type_on_get(_populated_env) -> None:
    """The GET must NOT carry Content-Type — some servers 415 on it."""
    with patch.object(
        policy_api_client.urllib.request,
        "urlopen",
        return_value=_ok_response(b'{"mode": "audit", "policies": ""}'),
    ) as mock_urlopen:
        fetch_policy_response()
    request_arg = mock_urlopen.call_args.args[0]
    assert request_arg.get_header("Content-type") is None
    assert request_arg.get_header("Accept") == "application/json"
    assert request_arg.get_header("Authorization") == "Bearer tok-abc"
    assert request_arg.get_header("X-uipath-internal-tenantid") == "tenant-1"
    assert request_arg.get_method() == "GET"


def test_url_includes_agent_type_when_set(_populated_env, monkeypatch) -> None:
    """``build_policy_url`` appends ``?agentType=...`` from the selector."""
    from uipath.runtime.governance.native import backend_client

    monkeypatch.setattr(backend_client, "_agent_is_conversational", True)
    url = build_policy_url("org-x")
    assert "agentType=conversational" in url


def test_url_omits_agent_type_when_unset(_populated_env, monkeypatch) -> None:
    from uipath.runtime.governance.native import backend_client

    monkeypatch.setattr(backend_client, "_agent_is_conversational", None)
    url = build_policy_url("org-x")
    assert "agentType=" not in url


# ---------------------------------------------------------------------------
# Body parser — _parse_policy_body
# ---------------------------------------------------------------------------


def test_parse_empty_body_returns_none() -> None:
    assert _parse_policy_body(b"") is None


def test_parse_non_utf8_body_returns_none() -> None:
    # 0xff isn't valid UTF-8.
    assert _parse_policy_body(b"\xff\xfe") is None


def test_parse_malformed_json_returns_none() -> None:
    # A common shape: server returns HTML when it should return JSON.
    assert _parse_policy_body(b"<html>oops</html>") is None


def test_parse_non_object_top_level_returns_none() -> None:
    """Server returning a bare JSON array is rejected — expected an object."""
    assert _parse_policy_body(b'["audit", "policies"]') is None


def test_parse_non_string_policies_field_returns_none() -> None:
    """``policies`` must be a string YAML body, not a number / dict / list."""
    assert _parse_policy_body(b'{"mode": "audit", "policies": 42}') is None


def test_parse_ok_yields_policy_response() -> None:
    resp = _parse_policy_body(
        b'{"mode": "enforce", "policies": "standard: p\\nrules: []"}'
    )
    assert resp is not None
    assert resp.mode == "enforce"
    assert "standard: p" in resp.policy


def test_parse_ok_with_missing_mode_yields_none_mode() -> None:
    """A response without ``mode`` is still valid — server may not override."""
    resp = _parse_policy_body(b'{"policies": ""}')
    assert resp is not None
    assert resp.mode is None
    assert resp.policy == ""


def test_parse_empty_string_mode_treated_as_unset() -> None:
    """Empty-string ``mode`` is normalized to ``None`` (don't override default)."""
    resp = _parse_policy_body(b'{"mode": "", "policies": ""}')
    assert resp is not None
    assert resp.mode is None


def test_parse_non_string_mode_treated_as_unset() -> None:
    """If the server sends mode as a number / null, treat as unset."""
    resp = _parse_policy_body(b'{"mode": 5, "policies": ""}')
    assert resp is not None
    assert resp.mode is None


# ---------------------------------------------------------------------------
# Full happy-path round-trip
# ---------------------------------------------------------------------------


def test_full_fetch_round_trip(_populated_env) -> None:
    body = json.dumps(
        {"mode": "audit", "policies": "standard: p\nrules: []"}
    ).encode("utf-8")
    with patch.object(
        policy_api_client.urllib.request,
        "urlopen",
        return_value=_ok_response(body),
    ):
        resp = fetch_policy_response()
    assert isinstance(resp, PolicyResponse)
    assert resp.mode == "audit"
    assert "standard: p" in resp.policy
