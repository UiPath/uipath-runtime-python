"""Tests for the conversational-vs-autonomous agent-type selector.

The governance wrapper records whether the hosted agent is conversational;
the policy fetch then appends an ``agentType`` query param so the server's
clause-resolver reads the matching container key (``*-in-flight-agents`` vs
``*-in-flight-conversational-agents``).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from uipath.runtime.governance.native import backend_client
from uipath.runtime.governance.native.backend_client import (
    agent_type_param,
    set_agent_conversational,
)
from uipath.runtime.governance.native.policy_api_client import build_policy_url

# The wrapper lands in a later slice of the governance stack; skip (don't
# error at collection) when it isn't present yet.
GovernanceRuntime = pytest.importorskip(
    "uipath.runtime.governance.wrapper",
    reason="GovernanceRuntime wrapper not yet present in this slice",
).GovernanceRuntime


def _extract(delegate, context=None) -> bool:
    """Call _extract_is_conversational without running __init__."""
    runtime = object.__new__(GovernanceRuntime)
    return runtime._extract_is_conversational(delegate, context)


@pytest.fixture(autouse=True)
def _reset_selector():
    """Clear the process-level selector around each test."""
    set_agent_conversational(None)
    yield
    set_agent_conversational(None)


def test_agent_type_param_unset_is_none():
    assert agent_type_param() is None


def test_agent_type_param_conversational():
    set_agent_conversational(True)
    assert agent_type_param() == "conversational"


def test_agent_type_param_autonomous():
    set_agent_conversational(False)
    assert agent_type_param() == "autonomous"


def test_build_policy_url_omits_param_when_unset(monkeypatch):
    monkeypatch.setattr(backend_client, "get_backend_base_url", lambda: "https://alpha.uipath.com")
    url = build_policy_url("my-org")
    assert url == "https://alpha.uipath.com/my-org/agenticgovernance_/api/v1/runtime/policy"
    assert "agentType" not in url


def test_build_policy_url_appends_conversational(monkeypatch):
    monkeypatch.setattr(backend_client, "get_backend_base_url", lambda: "https://alpha.uipath.com")
    set_agent_conversational(True)
    assert build_policy_url("my-org").endswith(
        "/my-org/agenticgovernance_/api/v1/runtime/policy?agentType=conversational"
    )


def test_build_policy_url_appends_autonomous(monkeypatch):
    monkeypatch.setattr(backend_client, "get_backend_base_url", lambda: "https://alpha.uipath.com")
    set_agent_conversational(False)
    assert build_policy_url("my-org").endswith("?agentType=autonomous")


# ── _extract_is_conversational ──────────────────────────────────────────────


def test_extract_conversational_from_agent_definition():
    delegate = SimpleNamespace(_agent_definition=SimpleNamespace(is_conversational=True))
    assert _extract(delegate) is True


def test_extract_autonomous_from_agent_definition():
    delegate = SimpleNamespace(_agent_definition=SimpleNamespace(is_conversational=False))
    assert _extract(delegate) is False


def test_extract_unwraps_delegate_chain():
    inner = SimpleNamespace(_agent_definition=SimpleNamespace(is_conversational=True))
    outer = SimpleNamespace(_delegate=inner)  # no _agent_definition on the outer
    assert _extract(outer) is True


def test_extract_falls_back_to_context_conversation_id():
    delegate = SimpleNamespace()  # nothing reachable
    context = SimpleNamespace(conversation_id="conv-1")
    assert _extract(delegate, context) is True


def test_extract_defaults_to_autonomous_when_unknown():
    assert _extract(SimpleNamespace(), SimpleNamespace()) is False