"""Tests for compensating governance calls to /runtime/govern.

The compensating call is fire-and-forget: the server runs the disabled
guardrail AND writes the audit trace itself, so we don't parse the
response. These tests cover:

- payload + header composition,
- URL resolution off the shared backend base URL,
- error swallowing (no exception escapes, warning is logged),
- evaluator integration (a fired ``guardrail_fallback`` rule kicks off
  the call on a background daemon thread).
"""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from uipath.core.governance.models import Action, LifecycleHook

from uipath.runtime.governance.config import (
    EnforcementMode,
    reset_enforcement_mode,
    set_enforcement_mode,
)
from uipath.runtime.governance.native import guardrail_compensation
from uipath.runtime.governance.native.backend_client import (
    USER_AGENT,
    governance_request_headers,
)
from uipath.runtime.governance.native.evaluator import GovernanceEvaluator
from uipath.runtime.governance.native.guardrail_compensation import (
    disabled_guardrails,
    request_governance,
)
from uipath.runtime.governance.native.models import (
    Check,
    CheckContext,
    Condition,
    PolicyIndex,
    PolicyPack,
    Rule,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_response(status: int = 200) -> MagicMock:
    """urlopen()-compatible context manager mock."""
    response = MagicMock()
    response.status = status
    response.read.return_value = b""  # body is not consumed by fire-and-forget
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    return response


def _rules(*validators: str, rule_id: str = "R1", rule_name: str = "n", pack: str = "p"):
    """Build the per-rule metadata list the compensation API now takes."""
    return [
        {
            "ruleId": rule_id,
            "ruleName": rule_name,
            "packName": pack,
            "validator": v,
        }
        for v in validators
    ]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_enforcement_mode():
    reset_enforcement_mode()
    yield
    reset_enforcement_mode()


@pytest.fixture
def _govern_env(monkeypatch):
    """Provide the org/tenant env vars that request_governance requires.

    The compensating call now mirrors the policy fetch — it skips when
    ``UIPATH_ORGANIZATION_ID`` / ``UIPATH_TENANT_ID`` are missing.
    Tests that need the network path to actually fire must opt into
    this fixture.
    """
    monkeypatch.setenv("UIPATH_ORGANIZATION_ID", "appsdev")
    monkeypatch.setenv("UIPATH_TENANT_ID", "tenant-xyz")
    yield


# ---------------------------------------------------------------------------
# Shared header helper (lives in backend_client; covered here because it's
# the wire shape both the compensation POST and the policy GET share)
# ---------------------------------------------------------------------------


def test_governance_request_headers_get_shape(monkeypatch):
    monkeypatch.delenv("UIPATH_ACCESS_TOKEN", raising=False)
    headers = governance_request_headers()
    assert headers == {"Accept": "application/json", "User-Agent": USER_AGENT}


def test_governance_request_headers_post_shape(monkeypatch):
    monkeypatch.delenv("UIPATH_ACCESS_TOKEN", raising=False)
    headers = governance_request_headers(json_body=True)
    assert headers == {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }


def test_governance_request_headers_includes_authorization_when_token_set(
    monkeypatch,
):
    monkeypatch.setenv("UIPATH_ACCESS_TOKEN", "abc.def.ghi")
    headers = governance_request_headers(json_body=True)
    assert headers["Authorization"] == "Bearer abc.def.ghi"


def test_governance_request_headers_user_agent_is_browser_shaped(monkeypatch):
    monkeypatch.delenv("UIPATH_ACCESS_TOKEN", raising=False)
    headers = governance_request_headers()
    assert headers["User-Agent"].startswith("Mozilla/5.0")
    assert "Chrome/" in headers["User-Agent"]


# ---------------------------------------------------------------------------
# request_governance — fire-and-forget contract
# ---------------------------------------------------------------------------


def test_request_governance_empty_types_short_circuits_without_call():
    with patch.object(
        guardrail_compensation.urllib.request, "urlopen"
    ) as mock_urlopen:
        result = request_governance(
            [], {}, "before_model", "t1", "2026-06-06T00:00:00Z", "agent", "rt"
        )
    assert result is None
    mock_urlopen.assert_not_called()


def test_request_governance_posts_expected_payload_and_returns_none(
    monkeypatch, _govern_env
):
    rules = [
        {
            "ruleId": "R-PII",
            "ruleName": "PII guardrail",
            "packName": "AITL",
            "validator": "pii_detection",
        },
        {
            "ruleId": "R-HARM",
            "ruleName": "Harmful content",
            "packName": "AITL",
            "validator": "harmful_content",
        },
    ]
    # Job context is resolved from UiPathConfig/env at call time; pin it so
    # the assertion is deterministic and exercises the new payload keys.
    monkeypatch.setattr(
        guardrail_compensation,
        "resolve_job_context",
        lambda: {"folderKey": "folder-1", "jobKey": "job-1"},
    )
    with patch.object(
        guardrail_compensation.urllib.request,
        "urlopen",
        return_value=_mock_response(),
    ) as mock_urlopen:
        result = request_governance(
            rules,
            {"content": "hello"},
            "before_model",
            "trace-1",
            "2026-06-06T00:00:00Z",
            "langchain",
            "patch-langchain",
        )

    assert result is None  # fire-and-forget

    request_arg = mock_urlopen.call_args.args[0]
    assert request_arg.get_method() == "POST"

    sent = json.loads(request_arg.data.decode("utf-8"))
    assert sent == {
        # distinct validators drive the guardrail API call
        "type": ["pii_detection", "harmful_content"],
        # per-rule metadata drives one trace record per rule
        "rules": rules,
        "data": {"content": "hello"},
        "hook": "before_model",
        "traceId": "trace-1",
        "src_timestamp": "2026-06-06T00:00:00Z",
        "agentName": "langchain",
        "runtimeId": "patch-langchain",
        "folderKey": "folder-1",
        "jobKey": "job-1",
    }


def test_request_governance_sends_shared_headers(monkeypatch, _govern_env):
    """Headers must come from the shared helper — UA + Accept + Content-Type."""
    monkeypatch.delenv("UIPATH_ACCESS_TOKEN", raising=False)
    with patch.object(
        guardrail_compensation.urllib.request,
        "urlopen",
        return_value=_mock_response(),
    ) as mock_urlopen:
        request_governance(
            _rules("x"), {}, "before_model", "t", "ts", "a", "r"
        )

    request_arg = mock_urlopen.call_args.args[0]
    # urllib title-cases header keys on the Request object.
    assert request_arg.get_header("Accept") == "application/json"
    assert request_arg.get_header("Content-type") == "application/json"
    assert request_arg.get_header("User-agent") == USER_AGENT
    # No token in env → no Authorization header.
    assert request_arg.get_header("Authorization") is None
    # Tenant header must travel on the compensating POST (same as the
    # policy GET) — the agenticgovernance ingress validates it.
    assert request_arg.get_header("X-uipath-internal-tenantid") == "tenant-xyz"


def test_request_governance_includes_bearer_token_when_set(monkeypatch, _govern_env):
    monkeypatch.setenv("UIPATH_ACCESS_TOKEN", "the-token")
    with patch.object(
        guardrail_compensation.urllib.request,
        "urlopen",
        return_value=_mock_response(),
    ) as mock_urlopen:
        request_governance(_rules("x"), {}, "before_model", "t", "ts", "a", "r")

    request_arg = mock_urlopen.call_args.args[0]
    assert request_arg.get_header("Authorization") == "Bearer the-token"


def test_request_governance_skipped_when_org_id_missing(monkeypatch):
    """Without an org id, we cannot build the URL — skip the call entirely."""
    monkeypatch.delenv("UIPATH_ORGANIZATION_ID", raising=False)
    monkeypatch.setenv("UIPATH_TENANT_ID", "tenant-xyz")
    with patch.object(
        guardrail_compensation.urllib.request, "urlopen"
    ) as mock_urlopen:
        request_governance(_rules("x"), {}, "before_model", "t", "ts", "a", "r")
    mock_urlopen.assert_not_called()


def test_request_governance_skipped_when_tenant_id_missing(monkeypatch):
    """Without a tenant id, the server's tenant header would be invalid."""
    monkeypatch.setenv("UIPATH_ORGANIZATION_ID", "appsdev")
    monkeypatch.delenv("UIPATH_TENANT_ID", raising=False)
    with patch.object(
        guardrail_compensation.urllib.request, "urlopen"
    ) as mock_urlopen:
        request_governance(_rules("x"), {}, "before_model", "t", "ts", "a", "r")
    mock_urlopen.assert_not_called()


def test_request_governance_swallows_network_error(_govern_env):
    """A network error must not propagate. (Log emission is logger-config
    dependent and is verified manually — the test-isolation behavior of
    pytest's caplog conflicts with the runtime's log interceptor.)"""
    with patch.object(
        guardrail_compensation.urllib.request,
        "urlopen",
        side_effect=OSError("connection refused"),
    ):
        result = request_governance(
            _rules("pii_detection"),
            {},
            "before_model",
            "t",
            "ts",
            "langchain",
            "patch-langchain",
        )

    assert result is None


def test_request_governance_swallows_unexpected_exception(_govern_env):
    """Even a programmer-error inside urlopen must not propagate."""
    with patch.object(
        guardrail_compensation.urllib.request,
        "urlopen",
        side_effect=RuntimeError("boom"),
    ):
        assert (
            request_governance(_rules("x"), {}, "before_model", "t", "ts", "a", "r")
            is None
        )


def test_request_governance_does_not_read_response_body(_govern_env):
    """Fire-and-forget: we must not consume the response body."""
    response = _mock_response()
    with patch.object(
        guardrail_compensation.urllib.request, "urlopen", return_value=response
    ):
        request_governance(_rules("x"), {}, "before_model", "t", "ts", "a", "r")
    response.read.assert_not_called()


def test_request_governance_url_is_org_scoped(monkeypatch, _govern_env):
    """URL must include the org segment and the agenticgovernance_ prefix.

    Mirrors the policy fetch URL shape — the agenticgovernance ingress
    requires both segments; without them the request lands on a route
    that doesn't exist (404 / wrong service).
    """
    monkeypatch.delenv("UIPATH_GOVERNANCE_BACKEND_URL", raising=False)
    monkeypatch.setenv("UIPATH_URL", "https://cloud.uipath.com/my-org/my-tenant")
    with patch.object(
        guardrail_compensation.urllib.request,
        "urlopen",
        return_value=_mock_response(),
    ) as mock_urlopen:
        request_governance(_rules("x"), {}, "before_model", "t", "ts", "a", "r")

    # org_id="appsdev" comes from the _govern_env fixture, not from UIPATH_URL
    # (UiPathConfig.organization_id is honoured first — same as policy).
    assert (
        mock_urlopen.call_args.args[0].full_url
        == "https://cloud.uipath.com/appsdev/agenticgovernance_/api/v1/runtime/govern"
    )


# ---------------------------------------------------------------------------
# submit_compensation — bounded background pool
# ---------------------------------------------------------------------------


def test_submit_compensation_empty_types_short_circuits():
    """submit_compensation with no types is a no-op (no semaphore taken)."""
    from uipath.runtime.governance.native.guardrail_compensation import (
        submit_compensation,
    )

    # Patch the executor to a MagicMock so we'd notice any spurious submit.
    with patch.object(guardrail_compensation, "_pool") as mock_pool:
        submit_compensation([], {}, "before_model", "t", "ts", "a", "r")
    mock_pool.submit.assert_not_called()


def test_submit_compensation_routes_through_pool():
    """A non-empty types list submits a single task to the pool."""
    from uipath.runtime.governance.native.guardrail_compensation import (
        submit_compensation,
    )

    with patch.object(guardrail_compensation, "_pool") as mock_pool:
        submit_compensation(
            _rules("pii_detection"),
            {"content": "x"},
            "before_model",
            "trace-1",
            "ts",
            "agent",
            "run",
        )
    mock_pool.submit.assert_called_once()


def test_submit_compensation_drops_when_pool_saturated(monkeypatch):
    """When the in-flight semaphore is exhausted, the call is dropped + logged."""
    from uipath.runtime.governance.native.guardrail_compensation import (
        submit_compensation,
    )

    # Force the semaphore into "exhausted" state.
    drained = threading.BoundedSemaphore(1)
    drained.acquire()  # value is now 0; next acquire(blocking=False) returns False
    monkeypatch.setattr(guardrail_compensation, "_inflight", drained)

    with patch.object(guardrail_compensation, "_pool") as mock_pool:
        submit_compensation(
            _rules("pii_detection"),
            {},
            "before_model",
            "trace-1",
            "ts",
            "agent",
            "run",
        )

    mock_pool.submit.assert_not_called()


def test_submit_compensation_swallows_pool_shutdown_runtimeerror(monkeypatch):
    """If the pool was shut down at process exit, submit must not raise."""
    from uipath.runtime.governance.native.guardrail_compensation import (
        submit_compensation,
    )

    # Fresh semaphore so we don't taint other tests.
    monkeypatch.setattr(
        guardrail_compensation, "_inflight", threading.BoundedSemaphore(4)
    )

    class _ShutdownPool:
        def submit(self, fn, *args, **kwargs):  # noqa: ARG002
            raise RuntimeError("cannot schedule new futures after shutdown")

    monkeypatch.setattr(guardrail_compensation, "_pool", _ShutdownPool())

    # Must not raise.
    submit_compensation(
        _rules("x"), {}, "before_model", "t", "ts", "a", "r"
    )


# ---------------------------------------------------------------------------
# disabled_guardrails
# ---------------------------------------------------------------------------


def test_disabled_guardrails_extracts_validators_for_fired_rules():
    cond = SimpleNamespace(
        operator="guardrail_fallback",
        value={
            "validator": "pii_detection",
            "mapped_to_uipath": True,
            "policy_enabled": False,
        },
    )
    rule = SimpleNamespace(checks=[SimpleNamespace(conditions=[cond])])
    audit = SimpleNamespace(
        evaluations=[
            SimpleNamespace(matched=True, rule_id="R1", rule_name="PII guardrail")
        ]
    )
    policy_index = SimpleNamespace(
        get_rule=lambda rid: rule if rid == "R1" else None
    )

    assert disabled_guardrails(audit, policy_index) == [
        {
            "ruleId": "R1",
            "ruleName": "PII guardrail",
            "packName": "",
            "validator": "pii_detection",
        }
    ]


def test_disabled_guardrails_skips_unmatched_evaluations():
    audit = SimpleNamespace(
        evaluations=[SimpleNamespace(matched=False, rule_id="R1", rule_name="x")]
    )
    policy_index = SimpleNamespace(get_rule=lambda rid: None)
    assert disabled_guardrails(audit, policy_index) == []


def test_disabled_guardrails_skips_non_guardrail_conditions():
    cond = SimpleNamespace(operator="regex", value="some-pattern")
    rule = SimpleNamespace(checks=[SimpleNamespace(conditions=[cond])])
    audit = SimpleNamespace(
        evaluations=[SimpleNamespace(matched=True, rule_id="R1", rule_name="x")]
    )
    policy_index = SimpleNamespace(get_rule=lambda rid: rule)
    assert disabled_guardrails(audit, policy_index) == []


# ---------------------------------------------------------------------------
# Evaluator integration: a guardrail_fallback rule kicks off the compensation
# ---------------------------------------------------------------------------


def _guardrail_fallback_rule() -> Rule:
    """A rule whose only check is a guardrail_fallback condition.

    Mirrors what ``_build_check`` produces for a YAML
    ``type: guardrail_fallback`` entry with the guardrail mapped to
    UiPath but disabled.
    """
    return Rule(
        rule_id="UIP-GR-01",
        name="PII guardrail (UiPath-mapped, disabled)",
        clause="UiPath-Mapped Guardrail",
        hook=LifecycleHook.BEFORE_MODEL,
        action=Action.AUDIT,
        checks=[
            Check(
                conditions=[
                    Condition(
                        operator="guardrail_fallback",
                        field="",
                        value={
                            "validator": "pii_detection",
                            "mapped_to_uipath": True,
                            "policy_enabled": False,
                        },
                    )
                ],
                action=Action.AUDIT,
                message="PII guardrail disabled",
            )
        ],
    )


def _build_index_with(rule: Rule) -> PolicyIndex:
    idx = PolicyIndex()
    idx.add_pack(
        PolicyPack(
            name="test_pack",
            version="1.0",
            description="test",
            rules=[rule],
        )
    )
    return idx


def test_evaluator_dispatches_compensation_for_fired_guardrail():
    """A matched guardrail_fallback rule must trigger request_governance."""
    set_enforcement_mode(EnforcementMode.AUDIT)
    evaluator = GovernanceEvaluator(_build_index_with(_guardrail_fallback_rule()))

    called = threading.Event()
    captured: dict[str, Any] = {}

    def _spy(**kwargs: Any) -> None:
        captured.update(kwargs)
        called.set()

    ctx = CheckContext(
        hook=LifecycleHook.BEFORE_MODEL,
        agent_name="agent-x",
        runtime_id="run-1",
        trace_id="trace-1",
        model_input="contact jane@acme.com",
    )

    with patch(
        "uipath.runtime.governance.native.evaluator.submit_compensation", _spy
    ):
        audit = evaluator.evaluate(ctx)

        assert called.wait(timeout=1.0), (
            "Expected request_governance to be called on a background thread"
        )

    assert audit.final_action == Action.AUDIT
    assert audit.rules_matched == 1
    assert captured["rules"] == [
        {
            "ruleId": "UIP-GR-01",
            "ruleName": "PII guardrail (UiPath-mapped, disabled)",
            "packName": "test_pack",
            "validator": "pii_detection",
        }
    ]
    assert captured["data"] == {"content": "contact jane@acme.com"}
    assert captured["hook"] == "before_model"
    assert captured["trace_id"] == "trace-1"
    assert captured["agent_name"] == "agent-x"
    assert captured["runtime_id"] == "run-1"
    assert isinstance(captured["src_timestamp"], str)
    assert "T" in captured["src_timestamp"]


def test_evaluator_does_not_dispatch_when_guardrail_is_enabled():
    rule = _guardrail_fallback_rule()
    rule.checks[0].conditions[0].value["policy_enabled"] = True  # type: ignore[index]

    set_enforcement_mode(EnforcementMode.AUDIT)
    evaluator = GovernanceEvaluator(_build_index_with(rule))

    called = threading.Event()

    def _spy(**kwargs: Any) -> None:
        called.set()

    ctx = CheckContext(
        hook=LifecycleHook.BEFORE_MODEL,
        agent_name="agent-x",
        runtime_id="run-1",
        trace_id="trace-1",
        model_input="hi",
    )

    with patch(
        "uipath.runtime.governance.native.evaluator.submit_compensation", _spy
    ):
        audit = evaluator.evaluate(ctx)
        time.sleep(0.05)

    assert not called.is_set()
    assert audit.rules_matched == 0


def test_evaluator_does_not_dispatch_when_not_mapped_to_uipath():
    rule = _guardrail_fallback_rule()
    rule.checks[0].conditions[0].value["mapped_to_uipath"] = False  # type: ignore[index]
    rule.checks[0].conditions[0].value["policy_enabled"] = False  # type: ignore[index]

    set_enforcement_mode(EnforcementMode.AUDIT)
    evaluator = GovernanceEvaluator(_build_index_with(rule))

    called = threading.Event()

    def _spy(**kwargs: Any) -> None:
        called.set()

    ctx = CheckContext(
        hook=LifecycleHook.BEFORE_MODEL,
        agent_name="agent-x",
        runtime_id="run-1",
        trace_id="trace-1",
        model_input="hi",
    )

    with patch(
        "uipath.runtime.governance.native.evaluator.submit_compensation", _spy
    ):
        evaluator.evaluate(ctx)
        time.sleep(0.05)

    assert not called.is_set()


def test_evaluator_compensation_dispatch_swallows_thread_errors():
    """If request_governance raises, the background thread must absorb it."""
    set_enforcement_mode(EnforcementMode.AUDIT)
    evaluator = GovernanceEvaluator(_build_index_with(_guardrail_fallback_rule()))

    def _raising_spy(**kwargs: Any) -> None:
        raise RuntimeError("network down")

    ctx = CheckContext(
        hook=LifecycleHook.BEFORE_MODEL,
        agent_name="agent-x",
        runtime_id="run-1",
        trace_id="trace-1",
        model_input="hi",
    )

    with patch(
        "uipath.runtime.governance.native.evaluator.submit_compensation",
        _raising_spy,
    ):
        audit = evaluator.evaluate(ctx)
        time.sleep(0.05)

    assert audit.final_action == Action.AUDIT
    assert audit.rules_matched == 1


def test_evaluator_does_not_emit_audit_trace_for_guardrail_fallback_rule():
    """Python must not emit a per-rule audit trace for ``guardrail_fallback``.

    The governance-server emits the trace in response to the
    ``/runtime/govern`` POST; emitting one here too would produce a
    duplicate. The rule still appears in the AuditRecord (so
    ``disabled_guardrails`` can find it) and the compensation thread
    still fires — only the per-rule ``rule_evaluation`` event is
    suppressed, and the hook summary's counts exclude it.
    """
    from uipath.runtime.governance.audit import (
        AuditEvent,
        AuditSink,
        EventType,
        get_audit_manager,
        reset_audit_manager,
    )

    class _CapturingSink(AuditSink):
        def __init__(self) -> None:
            self.events: list[AuditEvent] = []

        @property
        def name(self) -> str:
            return "capturing"

        def emit(self, event: AuditEvent) -> None:
            self.events.append(event)

    reset_audit_manager()
    try:
        manager = get_audit_manager()
        for existing in list(manager.list_sinks()):
            manager.unregister_sink(existing)
        sink = _CapturingSink()
        manager.register_sink(sink)
        manager._async_mode = False  # synchronous emission for assertions

        set_enforcement_mode(EnforcementMode.AUDIT)
        evaluator = GovernanceEvaluator(
            _build_index_with(_guardrail_fallback_rule())
        )

        ctx = CheckContext(
            hook=LifecycleHook.BEFORE_MODEL,
            agent_name="agent-x",
            runtime_id="run-1",
            trace_id="trace-1",
            model_input="hi",
        )

        # Stub the network call so it doesn't actually post; we're
        # asserting on the Python-emitted trace events, not on whether
        # /runtime/govern was reached.
        with patch(
            "uipath.runtime.governance.native.evaluator.submit_compensation",
            lambda **kwargs: None,
        ):
            audit = evaluator.evaluate(ctx)
            time.sleep(0.05)  # let the daemon thread land

        # The rule still matched and is in the audit record …
        assert audit.rules_matched == 1
        assert any(
            ev.matched and ev.rule_id == "UIP-GR-01" for ev in audit.evaluations
        )

        # … but NO rule_evaluation event for it was emitted by Python.
        rule_events = [
            e for e in sink.events if e.event_type == EventType.RULE_EVALUATION
        ]
        assert not any(
            e.data.get("rule_id") == "UIP-GR-01" for e in rule_events
        ), "guardrail_fallback rule must not emit a Python-side audit trace"

        # The hook summary's counts must also exclude the fallback rule
        # (so total_rules / matched_rules match what was actually emitted).
        summaries = [
            e for e in sink.events if e.event_type == EventType.HOOK_END
        ]
        assert len(summaries) == 1
        assert summaries[0].data["total_rules"] == 0
        assert summaries[0].data["matched_rules"] == 0
    finally:
        reset_audit_manager()
