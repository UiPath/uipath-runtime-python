"""Tests for ``GovernanceRuntime`` internal helpers.

The integration paths (full execute/stream/dispose) are covered in
``test_dispose_isolation.py`` and ``test_text_extraction.py``. This
file pins the smaller helpers that don't have dedicated coverage:
model-name / conversational extraction, agent-attribute discovery,
adapter attach + replacement, ``__getattr__`` forwarding, and the
module-level ``governance_wrapper`` / ``wrap_agent`` entry points.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from uipath.core.feature_flags import FeatureFlags

from uipath.runtime.governance import wrapper as wrapper_mod
from uipath.runtime.governance.config import (
    EnforcementMode,
    reset_enforcement_mode,
    set_enforcement_mode,
)
from uipath.runtime.governance.wrapper import (
    GovernanceRuntime,
    _current_model_name,
    get_current_model_name,
    governance_wrapper,
    wrap_agent,
)
from uipath.runtime.wrapper import GOVERNANCE_FEATURE_FLAG


@pytest.fixture
def _ff_on():
    """FF on AND mode=AUDIT — the path that actually wraps."""
    FeatureFlags.configure_flags({GOVERNANCE_FEATURE_FLAG: True})
    set_enforcement_mode(EnforcementMode.AUDIT)
    yield
    FeatureFlags.reset_flags()
    reset_enforcement_mode()


@pytest.fixture
def _ff_on_mode_disabled():
    """FF on but mode=DISABLED — wrappers should still short-circuit."""
    FeatureFlags.configure_flags({GOVERNANCE_FEATURE_FLAG: True})
    set_enforcement_mode(EnforcementMode.DISABLED)
    yield
    FeatureFlags.reset_flags()
    reset_enforcement_mode()


@pytest.fixture
def _ff_off():
    FeatureFlags.configure_flags({GOVERNANCE_FEATURE_FLAG: False})
    yield
    FeatureFlags.reset_flags()


def _runtime_init_off() -> GovernanceRuntime:
    """Build a GovernanceRuntime with FF off — fastest construction path.

    The FF-off path returns at line 153 without touching the network,
    so this is the right scaffold for testing per-method helpers
    that don't depend on the evaluator being materialised.
    """
    delegate = SimpleNamespace()
    return GovernanceRuntime(delegate=delegate, context=None, runtime_id="rt")


# ---------------------------------------------------------------------------
# get_current_model_name — module-level ContextVar accessor
# ---------------------------------------------------------------------------


def test_get_current_model_name_default_empty() -> None:
    """Unset ContextVar yields an empty string."""
    # Reset by binding empty.
    tok = _current_model_name.set("")
    try:
        assert get_current_model_name() == ""
    finally:
        _current_model_name.reset(tok)


def test_get_current_model_name_returns_bound_value() -> None:
    tok = _current_model_name.set("gpt-5")
    try:
        assert get_current_model_name() == "gpt-5"
    finally:
        _current_model_name.reset(tok)


# ---------------------------------------------------------------------------
# _extract_model_name — every fallback in order
# ---------------------------------------------------------------------------


def test_extract_model_name_from_agent_definition_settings() -> None:
    """LicensedRuntime pattern: delegate._agent_definition.settings.model."""
    runtime = _runtime_init_off()
    settings = SimpleNamespace(model="gpt-4o")
    delegate = SimpleNamespace(
        _agent_definition=SimpleNamespace(settings=settings),
    )
    assert runtime._extract_model_name(delegate, None) == "gpt-4o"


def test_extract_model_name_from_delegate_attribute() -> None:
    """Falls back to direct attributes on the delegate."""
    runtime = _runtime_init_off()
    delegate = SimpleNamespace(model="claude-sonnet")
    assert runtime._extract_model_name(delegate, None) == "claude-sonnet"


def test_extract_model_name_alternate_attribute_names() -> None:
    runtime = _runtime_init_off()
    for attr in ("model_name", "_model", "model_id"):
        delegate = SimpleNamespace(**{attr: f"via-{attr}"})
        assert runtime._extract_model_name(delegate, None) == f"via-{attr}"


def test_extract_model_name_via_nested_delegate_chain() -> None:
    """Unwraps wrapper layers via ``_delegate`` until it finds the definition."""
    runtime = _runtime_init_off()
    settings = SimpleNamespace(model="haiku")
    inner = SimpleNamespace(_agent_definition=SimpleNamespace(settings=settings))
    middle = SimpleNamespace(_delegate=inner)
    outer = SimpleNamespace(_delegate=middle)
    assert runtime._extract_model_name(outer, None) == "haiku"


def test_extract_model_name_via_public_delegate_attr() -> None:
    """``delegate.delegate`` (public) is also walked as part of the chain."""
    runtime = _runtime_init_off()
    settings = SimpleNamespace(model="opus")
    inner = SimpleNamespace(_agent_definition=SimpleNamespace(settings=settings))
    outer = SimpleNamespace(delegate=inner)  # public form
    assert runtime._extract_model_name(outer, None) == "opus"


def test_extract_model_name_from_context_fallback() -> None:
    """Last resort: read from the runtime context's attrs."""
    runtime = _runtime_init_off()
    delegate = SimpleNamespace()  # nothing extractable
    ctx = SimpleNamespace(model_name="ctx-model")
    assert runtime._extract_model_name(delegate, ctx) == "ctx-model"


def test_extract_model_name_returns_empty_when_unfindable() -> None:
    runtime = _runtime_init_off()
    assert runtime._extract_model_name(SimpleNamespace(), None) == ""


# ---------------------------------------------------------------------------
# _extract_is_conversational — agent_definition + context fallback + depth cap
# ---------------------------------------------------------------------------


def test_extract_is_conversational_from_agent_definition_true() -> None:
    runtime = _runtime_init_off()
    delegate = SimpleNamespace(
        _agent_definition=SimpleNamespace(is_conversational=True)
    )
    assert runtime._extract_is_conversational(delegate, None) is True


def test_extract_is_conversational_from_agent_definition_false() -> None:
    runtime = _runtime_init_off()
    delegate = SimpleNamespace(
        _agent_definition=SimpleNamespace(is_conversational=False)
    )
    assert runtime._extract_is_conversational(delegate, None) is False


def test_extract_is_conversational_from_nested_delegate() -> None:
    runtime = _runtime_init_off()
    inner = SimpleNamespace(
        _agent_definition=SimpleNamespace(is_conversational=True)
    )
    outer = SimpleNamespace(_delegate=inner)
    assert runtime._extract_is_conversational(outer, None) is True


def test_extract_is_conversational_fallback_to_conversation_id() -> None:
    """A populated ``conversation_id`` on the context implies a conv run."""
    runtime = _runtime_init_off()
    delegate = SimpleNamespace()
    ctx = SimpleNamespace(conversation_id="conv-abc")
    assert runtime._extract_is_conversational(delegate, ctx) is True


def test_extract_is_conversational_default_false() -> None:
    """No agent_def, no conversation_id → fail-safe to False (autonomous)."""
    runtime = _runtime_init_off()
    assert runtime._extract_is_conversational(SimpleNamespace(), None) is False


def test_extract_is_conversational_depth_cap_terminates() -> None:
    """A pathological wrapper chain doesn't loop forever — capped at 10."""
    runtime = _runtime_init_off()
    # Build a chain longer than the cap; none of the nodes carry an
    # _agent_definition. Must return False without hanging.
    node = SimpleNamespace()
    for _ in range(20):
        node = SimpleNamespace(_delegate=node)
    assert runtime._extract_is_conversational(node, None) is False


def test_extract_is_conversational_ignores_none_value_on_agent_def() -> None:
    """If ``is_conversational`` is None, look further up the chain."""
    runtime = _runtime_init_off()
    inner = SimpleNamespace(
        _agent_definition=SimpleNamespace(is_conversational=True)
    )
    outer = SimpleNamespace(
        _agent_definition=SimpleNamespace(is_conversational=None),
        _delegate=inner,
    )
    assert runtime._extract_is_conversational(outer, None) is True


# ---------------------------------------------------------------------------
# _extract_agent — finds agent via standard attribute names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "attr_name",
    ["_agent", "agent", "_runnable", "runnable", "_graph", "graph", "_chain", "chain", "_crew", "crew"],
)
def test_extract_agent_finds_via_known_attribute(attr_name: str) -> None:
    runtime = _runtime_init_off()
    agent_obj = object()
    delegate = SimpleNamespace(**{attr_name: agent_obj})
    agent, name = runtime._extract_agent(delegate)
    assert agent is agent_obj
    assert name == attr_name


def test_extract_agent_returns_first_priority_attribute() -> None:
    """``_agent`` wins over ``_graph`` when both are present."""
    runtime = _runtime_init_off()
    delegate = SimpleNamespace(_agent="primary", _graph="fallback")
    agent, name = runtime._extract_agent(delegate)
    assert agent == "primary"
    assert name == "_agent"


def test_extract_agent_via_nested_delegate_chain() -> None:
    runtime = _runtime_init_off()
    agent_obj = object()
    inner = SimpleNamespace(_agent=agent_obj)
    outer = SimpleNamespace(_delegate=inner)
    agent, name = runtime._extract_agent(outer)
    assert agent is agent_obj
    assert name == "_agent"
    # ``_agent_holder`` is set to the inner delegate so dispose can
    # restore the original agent on the right object.
    assert runtime._agent_holder is inner


def test_extract_agent_returns_none_when_not_found() -> None:
    runtime = _runtime_init_off()
    delegate = SimpleNamespace(unrelated="thing")
    agent, name = runtime._extract_agent(delegate)
    assert agent is None
    assert name is None


def test_extract_agent_nested_depth_cap() -> None:
    """A deep chain without any matching attribute terminates cleanly."""
    runtime = _runtime_init_off()
    node = SimpleNamespace()
    for _ in range(15):
        node = SimpleNamespace(_delegate=node)
    agent, name = runtime._extract_agent(node)
    assert agent is None
    assert name is None


# ---------------------------------------------------------------------------
# __getattr__ forwarding
# ---------------------------------------------------------------------------


def test_getattr_forwards_public_attributes_to_delegate() -> None:
    """Unknown public attrs are forwarded to the wrapped runtime."""
    delegate = SimpleNamespace(custom_value="from-delegate", another=42)
    runtime = GovernanceRuntime(delegate=delegate, context=None, runtime_id="rt")
    # Direct attribute access falls through to __getattr__ when the
    # name isn't on GovernanceRuntime itself.
    assert runtime.custom_value == "from-delegate"
    assert runtime.another == 42


def test_getattr_raises_for_private_names() -> None:
    """Private attribute names (``_foo``) are not forwarded — they raise."""
    delegate = SimpleNamespace(_should_not_forward="x")
    runtime = GovernanceRuntime(delegate=delegate, context=None, runtime_id="rt")
    with pytest.raises(AttributeError):
        _ = runtime._should_not_forward


def test_getattr_raises_when_delegate_missing_attr() -> None:
    """Attribute the delegate doesn't have raises AttributeError."""
    runtime = GovernanceRuntime(
        delegate=SimpleNamespace(), context=None, runtime_id="rt"
    )
    with pytest.raises(AttributeError):
        _ = runtime.nonexistent_attr


# ---------------------------------------------------------------------------
# governance_wrapper module function — the gate
# ---------------------------------------------------------------------------


def test_governance_wrapper_returns_unwrapped_when_ff_off(_ff_off) -> None:
    """FF off → returns the original runtime untouched."""
    original = SimpleNamespace(unique="marker")
    result = governance_wrapper(original, context=None, runtime_id="rt")
    assert result is original


def test_governance_wrapper_returns_unwrapped_when_mode_disabled(
    _ff_on_mode_disabled,
) -> None:
    """FF on but enforcement mode is DISABLED → returns runtime unchanged.

    The DISABLED short-circuit avoids per-call audit overhead for
    tenants whose server has explicitly opted out of governance.
    """
    original = SimpleNamespace()
    result = governance_wrapper(original, context=None, runtime_id="rt")
    assert result is original
    assert not isinstance(result, GovernanceRuntime)


def test_governance_wrapper_returns_governance_runtime_when_ff_on(_ff_on) -> None:
    """FF on + mode != DISABLED → wraps in GovernanceRuntime."""
    original = SimpleNamespace()
    result = governance_wrapper(original, context=None, runtime_id="rt")
    assert isinstance(result, GovernanceRuntime)
    # The unwrapped runtime is accessible via .delegate.
    assert result.delegate is original


# ---------------------------------------------------------------------------
# wrap_agent module function — direct adapter wrap path
# ---------------------------------------------------------------------------


def test_wrap_agent_returns_original_when_ff_off(_ff_off) -> None:
    """FF off → returns the agent unchanged."""
    agent = SimpleNamespace(marker="x")
    result = wrap_agent(agent, agent_id="a")
    assert result is agent


def test_wrap_agent_returns_original_when_mode_disabled(
    _ff_on_mode_disabled,
) -> None:
    """FF on but mode=DISABLED → returns the agent unchanged."""
    agent = SimpleNamespace(marker="x")
    result = wrap_agent(agent, agent_id="a")
    assert result is agent


def test_wrap_agent_returns_original_when_no_adapter(_ff_on) -> None:
    """FF on, mode=AUDIT, but no adapter matches → returns the agent unchanged."""
    agent = SimpleNamespace(marker="x")
    fake_registry = MagicMock()
    fake_registry.resolve.return_value = None
    with patch.object(wrapper_mod, "get_adapter_registry", return_value=fake_registry):
        with patch.object(wrapper_mod, "get_policy_index", return_value=MagicMock()):
            result = wrap_agent(agent, agent_id="a")
    assert result is agent


def test_wrap_agent_attaches_when_adapter_found(_ff_on) -> None:
    """When an adapter is found, ``adapter.attach`` is called and result returned."""
    agent = SimpleNamespace(marker="x")
    governed_agent = SimpleNamespace(governed=True)
    fake_adapter = MagicMock()
    fake_adapter.attach.return_value = governed_agent
    fake_adapter.name = "test-adapter"
    fake_registry = MagicMock()
    fake_registry.resolve.return_value = fake_adapter

    with patch.object(wrapper_mod, "get_adapter_registry", return_value=fake_registry):
        with patch.object(wrapper_mod, "get_policy_index", return_value=MagicMock()):
            result = wrap_agent(agent, agent_id="my-agent")

    assert result is governed_agent
    fake_adapter.attach.assert_called_once()
    call_kwargs = fake_adapter.attach.call_args.kwargs
    assert call_kwargs["agent_id"] == "my-agent"
    # session_id is generated internally as a uuid4 string.
    assert isinstance(call_kwargs["session_id"], str)
    assert len(call_kwargs["session_id"]) > 0


def test_wrap_agent_defaults_agent_id_to_class_name(_ff_on) -> None:
    """When ``agent_id`` is None, falls back to ``type(agent).__name__``."""

    class MyAgent:
        marker = "x"

    agent = MyAgent()
    fake_adapter = MagicMock()
    fake_adapter.attach.return_value = SimpleNamespace()
    fake_adapter.name = "test-adapter"
    fake_registry = MagicMock()
    fake_registry.resolve.return_value = fake_adapter

    with patch.object(wrapper_mod, "get_adapter_registry", return_value=fake_registry):
        with patch.object(wrapper_mod, "get_policy_index", return_value=MagicMock()):
            wrap_agent(agent)  # no agent_id
    assert fake_adapter.attach.call_args.kwargs["agent_id"] == "MyAgent"
