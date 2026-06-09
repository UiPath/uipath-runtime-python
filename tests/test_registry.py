import sys
import tempfile
import types
from pathlib import Path
from typing import Any, AsyncGenerator, Optional, cast
from unittest.mock import MagicMock

import pytest
from uipath.core.feature_flags import FeatureFlags

from uipath.runtime import (
    GOVERNANCE_FEATURE_FLAG,
    UiPathExecuteOptions,
    UiPathRuntimeContext,
    UiPathRuntimeEvent,
    UiPathRuntimeFactoryProtocol,
    UiPathRuntimeFactoryRegistry,
    UiPathRuntimeFactorySettings,
    UiPathRuntimeProtocol,
    UiPathRuntimeResult,
    UiPathRuntimeSchema,
    UiPathRuntimeStatus,
    UiPathRuntimeStorageProtocol,
    UiPathStreamOptions,
)
from uipath.runtime.registry import UiPathWrappedRuntimeFactory


class MockStorage(UiPathRuntimeStorageProtocol):
    """Mock storage implementation"""

    def __init__(self):
        self._store = {}

    async def set_value(self, runtime_id, namespace, key, value):
        self._store.setdefault(runtime_id, {}).setdefault(namespace, {})[key] = value

    async def get_value(self, runtime_id, namespace, key):
        return self._store.get(runtime_id, {}).get(namespace, {}).get(key)


class MockRuntime(UiPathRuntimeProtocol):
    """Mock runtime instance"""

    def __init__(self, name: str):
        self.name = name

    async def execute(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathExecuteOptions | None = None,
    ) -> UiPathRuntimeResult:
        return UiPathRuntimeResult(
            output={"runtime": "mock"}, status=UiPathRuntimeStatus.SUCCESSFUL
        )

    async def get_schema(self) -> UiPathRuntimeSchema:
        """NotImplemented"""
        raise NotImplementedError()

    async def stream(
        self,
        input: dict[str, Any] | None = None,
        options: UiPathStreamOptions | None = None,
    ) -> AsyncGenerator[UiPathRuntimeEvent, None]:
        raise NotImplementedError()
        yield

    async def dispose(self) -> None:
        pass


class MockFunctionsFactory(UiPathRuntimeFactoryProtocol):
    """Mock factory for uipath.json (default)"""

    def __init__(self, context: Optional[UiPathRuntimeContext] = None):
        self.context = context
        self.name = "functions"

    def discover_entrypoints(self) -> list[str]:
        return ["main.py", "handler.py"]

    async def get_storage(self) -> UiPathRuntimeStorageProtocol | None:
        return MockStorage()

    async def get_settings(self) -> UiPathRuntimeFactorySettings | None:
        return UiPathRuntimeFactorySettings()

    async def new_runtime(
        self, entrypoint: str, runtime_id: str, **kwargs
    ) -> UiPathRuntimeProtocol:
        return cast(UiPathRuntimeProtocol, MockRuntime(f"functions-{entrypoint}"))

    async def dispose(self) -> None:
        pass


class MockLangGraphFactory(UiPathRuntimeFactoryProtocol):
    """Mock factory for langgraph.json"""

    def __init__(self, context: Optional[UiPathRuntimeContext] = None):
        self.context = context
        self.name = "langgraph"

    def discover_entrypoints(self) -> list[str]:
        return ["agent", "workflow"]

    async def get_storage(self) -> UiPathRuntimeStorageProtocol | None:
        return MockStorage()

    async def get_settings(self) -> UiPathRuntimeFactorySettings | None:
        return UiPathRuntimeFactorySettings()

    async def new_runtime(
        self, entrypoint: str, runtime_id: str, **kwargs
    ) -> UiPathRuntimeProtocol:
        return cast(UiPathRuntimeProtocol, MockRuntime(f"langgraph-{entrypoint}"))

    async def dispose(self) -> None:
        pass


class MockLlamaIndexFactory(UiPathRuntimeFactoryProtocol):
    """Mock factory for llamaindex.json"""

    def __init__(self, context: Optional[UiPathRuntimeContext] = None):
        self.context = context
        self.name = "llamaindex"

    def discover_entrypoints(self) -> list[str]:
        return ["chatbot", "rag"]

    async def get_storage(self) -> UiPathRuntimeStorageProtocol | None:
        return MockStorage()

    async def get_settings(self) -> UiPathRuntimeFactorySettings | None:
        return UiPathRuntimeFactorySettings()

    async def new_runtime(
        self, entrypoint: str, runtime_id: str, **kwargs
    ) -> UiPathRuntimeProtocol:
        return cast(UiPathRuntimeProtocol, MockRuntime(f"llamaindex-{entrypoint}"))

    async def dispose(self) -> None:
        pass


@pytest.fixture
def clean_registry():
    """Clean registry before and after each test"""
    UiPathRuntimeFactoryRegistry._factories = {}
    UiPathRuntimeFactoryRegistry._registration_order = []
    UiPathRuntimeFactoryRegistry._default_name = None
    yield
    UiPathRuntimeFactoryRegistry._factories = {}
    UiPathRuntimeFactoryRegistry._registration_order = []
    UiPathRuntimeFactoryRegistry._default_name = None


@pytest.fixture
def temp_dir():
    """Create a temporary directory for config files"""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


def test_register_single_factory(clean_registry):
    """Test registering a single factory"""

    def create_factory(
        context: UiPathRuntimeContext | None = None,
    ) -> UiPathRuntimeFactoryProtocol:
        return MockFunctionsFactory(context)

    UiPathRuntimeFactoryRegistry.register("functions", create_factory, "uipath.json")

    all_factories = UiPathRuntimeFactoryRegistry.get_all()
    assert "functions" in all_factories
    assert all_factories["functions"] == "uipath.json"


def test_register_multiple_factories(clean_registry):
    """Test registering multiple factories"""

    def create_functions(
        context: UiPathRuntimeContext | None = None,
    ) -> UiPathRuntimeFactoryProtocol:
        return MockFunctionsFactory(context)

    def create_langgraph(
        context: UiPathRuntimeContext | None = None,
    ) -> UiPathRuntimeFactoryProtocol:
        return MockLangGraphFactory(context)

    def create_llamaindex(
        context: UiPathRuntimeContext | None = None,
    ) -> UiPathRuntimeFactoryProtocol:
        return MockLlamaIndexFactory(context)

    UiPathRuntimeFactoryRegistry.register("functions", create_functions, "uipath.json")
    UiPathRuntimeFactoryRegistry.register(
        "langgraph", create_langgraph, "langgraph.json"
    )
    UiPathRuntimeFactoryRegistry.register(
        "llamaindex", create_llamaindex, "llamaindex.json"
    )

    all_factories = UiPathRuntimeFactoryRegistry.get_all()
    assert len(all_factories) == 3
    assert all_factories["functions"] == "uipath.json"
    assert all_factories["langgraph"] == "langgraph.json"
    assert all_factories["llamaindex"] == "llamaindex.json"


def test_set_default_factory(clean_registry):
    """Test setting a default factory"""

    def create_factory(
        context: UiPathRuntimeContext | None = None,
    ) -> UiPathRuntimeFactoryProtocol:
        return MockFunctionsFactory(context)

    UiPathRuntimeFactoryRegistry.register("functions", create_factory, "uipath.json")
    UiPathRuntimeFactoryRegistry.set_default("functions")

    assert UiPathRuntimeFactoryRegistry._default_name == "functions"


def test_set_default_nonexistent_factory(clean_registry):
    """Test setting default for non-existent factory raises error"""
    with pytest.raises(ValueError, match="Factory 'nonexistent' not registered"):
        UiPathRuntimeFactoryRegistry.set_default("nonexistent")


def test_get_factory_by_name(clean_registry):
    """Test getting factory by name"""

    def create_factory(
        context: UiPathRuntimeContext | None = None,
    ) -> UiPathRuntimeFactoryProtocol:
        return MockLangGraphFactory(context)

    UiPathRuntimeFactoryRegistry.register("langgraph", create_factory, "langgraph.json")

    factory = UiPathRuntimeFactoryRegistry.get(name="langgraph", apply_wrappers=False)
    assert isinstance(factory, MockLangGraphFactory)
    assert factory.name == "langgraph"


def test_get_factory_by_name_with_context(clean_registry):
    """Test getting factory by name with context"""

    def create_factory(
        context: UiPathRuntimeContext | None = None,
    ) -> UiPathRuntimeFactoryProtocol:
        return MockLangGraphFactory(context)

    UiPathRuntimeFactoryRegistry.register("langgraph", create_factory, "langgraph.json")

    context = UiPathRuntimeContext.with_defaults(entrypoint="test")
    factory = UiPathRuntimeFactoryRegistry.get(
        name="langgraph", context=context, apply_wrappers=False
    )
    assert isinstance(factory, MockLangGraphFactory)
    assert factory.context == context


def test_get_nonexistent_factory_by_name(clean_registry):
    """Test getting non-existent factory by name raises error"""
    with pytest.raises(ValueError, match="Factory 'nonexistent' not registered"):
        UiPathRuntimeFactoryRegistry.get(name="nonexistent")


def test_auto_detect_langgraph_json(clean_registry, temp_dir):
    """Test auto-detection with langgraph.json present"""

    def create_functions(
        context: UiPathRuntimeContext | None = None,
    ) -> UiPathRuntimeFactoryProtocol:
        return MockFunctionsFactory(context)

    def create_langgraph(
        context: UiPathRuntimeContext | None = None,
    ) -> UiPathRuntimeFactoryProtocol:
        return MockLangGraphFactory(context)

    UiPathRuntimeFactoryRegistry.register("functions", create_functions, "uipath.json")
    UiPathRuntimeFactoryRegistry.register(
        "langgraph", create_langgraph, "langgraph.json"
    )
    UiPathRuntimeFactoryRegistry.set_default("functions")

    Path(temp_dir, "langgraph.json").touch()

    factory = UiPathRuntimeFactoryRegistry.get(
        search_path=temp_dir, apply_wrappers=False
    )
    assert isinstance(factory, MockLangGraphFactory)


def test_auto_detect_llamaindex_json(clean_registry, temp_dir):
    """Test auto-detection with llamaindex.json present"""

    def create_functions(
        context: UiPathRuntimeContext | None = None,
    ) -> UiPathRuntimeFactoryProtocol:
        return MockFunctionsFactory(context)

    def create_llamaindex(
        context: UiPathRuntimeContext | None = None,
    ) -> UiPathRuntimeFactoryProtocol:
        return MockLlamaIndexFactory(context)

    UiPathRuntimeFactoryRegistry.register("functions", create_functions, "uipath.json")
    UiPathRuntimeFactoryRegistry.register(
        "llamaindex", create_llamaindex, "llamaindex.json"
    )
    UiPathRuntimeFactoryRegistry.set_default("functions")

    Path(temp_dir, "llamaindex.json").touch()

    factory = UiPathRuntimeFactoryRegistry.get(
        search_path=temp_dir, apply_wrappers=False
    )
    assert isinstance(factory, MockLlamaIndexFactory)


def test_auto_detect_uipath_json(clean_registry, temp_dir):
    """Test auto-detection with uipath.json present"""

    def create_functions(
        context: UiPathRuntimeContext | None = None,
    ) -> UiPathRuntimeFactoryProtocol:
        return MockFunctionsFactory(context)

    def create_langgraph(
        context: UiPathRuntimeContext | None = None,
    ) -> UiPathRuntimeFactoryProtocol:
        return MockLangGraphFactory(context)

    UiPathRuntimeFactoryRegistry.register("functions", create_functions, "uipath.json")
    UiPathRuntimeFactoryRegistry.register(
        "langgraph", create_langgraph, "langgraph.json"
    )
    UiPathRuntimeFactoryRegistry.set_default("functions")

    Path(temp_dir, "uipath.json").touch()

    factory = UiPathRuntimeFactoryRegistry.get(
        search_path=temp_dir, apply_wrappers=False
    )
    assert isinstance(factory, MockFunctionsFactory)


def test_fallback_to_default(clean_registry, temp_dir):
    """Test fallback to default factory when no config file found"""

    def create_functions(
        context: UiPathRuntimeContext | None = None,
    ) -> UiPathRuntimeFactoryProtocol:
        return MockFunctionsFactory(context)

    def create_langgraph(
        context: UiPathRuntimeContext | None = None,
    ) -> UiPathRuntimeFactoryProtocol:
        return MockLangGraphFactory(context)

    UiPathRuntimeFactoryRegistry.register("functions", create_functions, "uipath.json")
    UiPathRuntimeFactoryRegistry.register(
        "langgraph", create_langgraph, "langgraph.json"
    )
    UiPathRuntimeFactoryRegistry.set_default("functions")

    factory = UiPathRuntimeFactoryRegistry.get(
        search_path=temp_dir, apply_wrappers=False
    )
    assert isinstance(factory, MockFunctionsFactory)


def test_no_default_no_config_raises_error(clean_registry, temp_dir):
    """Test error when no default and no config file found"""

    def create_factory(
        context: UiPathRuntimeContext | None = None,
    ) -> UiPathRuntimeFactoryProtocol:
        return MockFunctionsFactory(context)

    UiPathRuntimeFactoryRegistry.register("functions", create_factory, "uipath.json")

    with pytest.raises(
        ValueError, match="No default factory registered and no config file found"
    ):
        UiPathRuntimeFactoryRegistry.get(search_path=temp_dir)


def test_priority_langgraph_over_uipath(clean_registry, temp_dir):
    """Test that langgraph.json takes priority when both exist"""

    def create_functions(
        context: UiPathRuntimeContext | None = None,
    ) -> UiPathRuntimeFactoryProtocol:
        return MockFunctionsFactory(context)

    def create_langgraph(
        context: UiPathRuntimeContext | None = None,
    ) -> UiPathRuntimeFactoryProtocol:
        return MockLangGraphFactory(context)

    UiPathRuntimeFactoryRegistry.register("functions", create_functions, "uipath.json")
    UiPathRuntimeFactoryRegistry.register(
        "langgraph", create_langgraph, "langgraph.json"
    )
    UiPathRuntimeFactoryRegistry.set_default("functions")

    Path(temp_dir, "uipath.json").touch()
    Path(temp_dir, "langgraph.json").touch()

    factory = UiPathRuntimeFactoryRegistry.get(
        search_path=temp_dir, apply_wrappers=False
    )
    assert isinstance(factory, MockLangGraphFactory)


@pytest.mark.asyncio
async def test_factory_discover_entrypoints(clean_registry):
    """Test factory can discover entrypoints"""

    def create_factory(
        context: UiPathRuntimeContext | None = None,
    ) -> UiPathRuntimeFactoryProtocol:
        return MockLangGraphFactory(context)

    UiPathRuntimeFactoryRegistry.register("langgraph", create_factory, "langgraph.json")

    factory = UiPathRuntimeFactoryRegistry.get(name="langgraph")
    entrypoints = factory.discover_entrypoints()
    assert entrypoints == ["agent", "workflow"]


@pytest.mark.asyncio
async def test_factory_create_runtime(clean_registry):
    """Test factory can create runtime instances"""

    def create_factory(
        context: UiPathRuntimeContext | None = None,
    ) -> UiPathRuntimeFactoryProtocol:
        return MockLangGraphFactory(context)

    UiPathRuntimeFactoryRegistry.register("langgraph", create_factory, "langgraph.json")

    factory = UiPathRuntimeFactoryRegistry.get(name="langgraph", apply_wrappers=False)
    runtime = await factory.new_runtime("agent", "runtime-1")
    assert isinstance(runtime, MockRuntime)
    assert runtime.name == "langgraph-agent"


def test_get_all_returns_copy(clean_registry):
    """Test that get_all returns a copy, not the internal dict"""

    def create_factory(
        context: UiPathRuntimeContext | None = None,
    ) -> UiPathRuntimeFactoryProtocol:
        return MockFunctionsFactory(context)

    UiPathRuntimeFactoryRegistry.register("functions", create_factory, "uipath.json")

    all_factories = UiPathRuntimeFactoryRegistry.get_all()
    all_factories["malicious"] = "hack.json"

    assert "malicious" not in UiPathRuntimeFactoryRegistry.get_all()


# ---------------------------------------------------------------------------
# Wrapping behaviour (apply_wrappers=True)
# ---------------------------------------------------------------------------


@pytest.fixture
def _reset_feature_flags():
    FeatureFlags.reset_flags()
    yield
    FeatureFlags.reset_flags()


@pytest.fixture
def fake_governance_module(monkeypatch):
    """Install a stub ``uipath.runtime.governance.wrapper`` for the lazy import.

    ``apply_governance_wrapper`` imports ``governance_wrapper`` only when
    the FF is on; this fixture lets us assert it was called without
    triggering the real governance runtime (which would try to talk to
    the policy backend).
    """
    mock_wrapper = MagicMock(name="governance_wrapper")
    module = types.ModuleType("uipath.runtime.governance.wrapper")
    module.governance_wrapper = mock_wrapper
    monkeypatch.setitem(sys.modules, "uipath.runtime.governance.wrapper", module)
    return mock_wrapper


def test_get_returns_wrapped_factory_by_default(clean_registry):
    """``get`` with default args wraps the registered factory."""

    def create_factory(
        context: UiPathRuntimeContext | None = None,
    ) -> UiPathRuntimeFactoryProtocol:
        return MockLangGraphFactory(context)

    UiPathRuntimeFactoryRegistry.register("langgraph", create_factory, "langgraph.json")

    factory = UiPathRuntimeFactoryRegistry.get(name="langgraph")

    assert isinstance(factory, UiPathWrappedRuntimeFactory)
    assert isinstance(factory.inner, MockLangGraphFactory)


def test_wrapped_factory_attribute_fallthrough(clean_registry):
    """Non-protocol attributes on the delegate are reachable via ``__getattr__``."""

    def create_factory(
        context: UiPathRuntimeContext | None = None,
    ) -> UiPathRuntimeFactoryProtocol:
        return MockLangGraphFactory(context)

    UiPathRuntimeFactoryRegistry.register("langgraph", create_factory, "langgraph.json")

    factory = UiPathRuntimeFactoryRegistry.get(name="langgraph")

    # `name` is not part of the protocol but is defined on the concrete factory.
    assert factory.name == "langgraph"  # type: ignore[attr-defined]


def test_wrapped_factory_delegates_protocol_methods(clean_registry):
    """Protocol methods reach the underlying factory unchanged."""

    def create_factory(
        context: UiPathRuntimeContext | None = None,
    ) -> UiPathRuntimeFactoryProtocol:
        return MockLangGraphFactory(context)

    UiPathRuntimeFactoryRegistry.register("langgraph", create_factory, "langgraph.json")

    factory = UiPathRuntimeFactoryRegistry.get(name="langgraph")

    assert factory.discover_entrypoints() == ["agent", "workflow"]


@pytest.mark.asyncio
async def test_wrapped_factory_returns_inner_runtime_when_flag_off(
    clean_registry, _reset_feature_flags, monkeypatch
):
    """With the governance FF off, ``new_runtime`` returns the bare runtime."""
    FeatureFlags.configure_flags({GOVERNANCE_FEATURE_FLAG: False})
    monkeypatch.delenv("UIPATH_FEATURE_" + GOVERNANCE_FEATURE_FLAG, raising=False)

    def create_factory(
        context: UiPathRuntimeContext | None = None,
    ) -> UiPathRuntimeFactoryProtocol:
        return MockLangGraphFactory(context)

    UiPathRuntimeFactoryRegistry.register("langgraph", create_factory, "langgraph.json")

    factory = UiPathRuntimeFactoryRegistry.get(name="langgraph")
    runtime = await factory.new_runtime("agent", "runtime-1")

    assert isinstance(runtime, MockRuntime)
    assert runtime.name == "langgraph-agent"


@pytest.mark.asyncio
async def test_wrapped_factory_invokes_governance_when_flag_on(
    clean_registry, _reset_feature_flags, fake_governance_module
):
    """With the governance FF on, ``new_runtime`` routes through ``governance_wrapper``."""
    FeatureFlags.configure_flags({GOVERNANCE_FEATURE_FLAG: True})

    governed_sentinel = MagicMock(name="GovernedRuntime")
    fake_governance_module.return_value = governed_sentinel

    captured: dict[str, Any] = {}

    def create_factory(
        context: UiPathRuntimeContext | None = None,
    ) -> UiPathRuntimeFactoryProtocol:
        captured["context"] = context
        return MockLangGraphFactory(context)

    UiPathRuntimeFactoryRegistry.register("langgraph", create_factory, "langgraph.json")

    context = UiPathRuntimeContext.with_defaults(entrypoint="agent")
    factory = UiPathRuntimeFactoryRegistry.get(name="langgraph", context=context)

    result = await factory.new_runtime("agent", "runtime-42")

    assert result is governed_sentinel
    fake_governance_module.assert_called_once()
    call_args = fake_governance_module.call_args
    # signature: governance_wrapper(runtime, context, runtime_id)
    assert isinstance(call_args.args[0], MockRuntime)
    assert call_args.args[1] is context
    assert call_args.args[2] == "runtime-42"


@pytest.mark.asyncio
async def test_wrapped_factory_swallows_governance_exception(
    clean_registry, _reset_feature_flags, fake_governance_module
):
    """Governance failures must never break runtime creation."""
    FeatureFlags.configure_flags({GOVERNANCE_FEATURE_FLAG: True})
    fake_governance_module.side_effect = RuntimeError("policy load failed")

    def create_factory(
        context: UiPathRuntimeContext | None = None,
    ) -> UiPathRuntimeFactoryProtocol:
        return MockLangGraphFactory(context)

    UiPathRuntimeFactoryRegistry.register("langgraph", create_factory, "langgraph.json")

    factory = UiPathRuntimeFactoryRegistry.get(name="langgraph")
    runtime = await factory.new_runtime("agent", "runtime-1")

    assert isinstance(runtime, MockRuntime)
