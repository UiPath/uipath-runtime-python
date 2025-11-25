import tempfile
from pathlib import Path
from typing import Any, AsyncGenerator, Optional, cast

import pytest

from uipath.runtime import (
    UiPathExecuteOptions,
    UiPathRuntimeContext,
    UiPathRuntimeEvent,
    UiPathRuntimeFactoryProtocol,
    UiPathRuntimeFactoryRegistry,
    UiPathRuntimeProtocol,
    UiPathRuntimeResult,
    UiPathRuntimeSchema,
    UiPathRuntimeStatus,
    UiPathStreamOptions,
)


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

    async def discover_runtimes(self) -> list[UiPathRuntimeProtocol]:
        return []

    async def new_runtime(
        self, entrypoint: str, runtime_id: str
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

    async def discover_runtimes(self) -> list[UiPathRuntimeProtocol]:
        return []

    async def new_runtime(
        self, entrypoint: str, runtime_id: str
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

    async def discover_runtimes(self) -> list[UiPathRuntimeProtocol]:
        return []

    async def new_runtime(
        self, entrypoint: str, runtime_id: str
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

    factory = UiPathRuntimeFactoryRegistry.get(name="langgraph")
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
    factory = UiPathRuntimeFactoryRegistry.get(name="langgraph", context=context)
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

    factory = UiPathRuntimeFactoryRegistry.get(search_path=temp_dir)
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

    factory = UiPathRuntimeFactoryRegistry.get(search_path=temp_dir)
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

    factory = UiPathRuntimeFactoryRegistry.get(search_path=temp_dir)
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

    factory = UiPathRuntimeFactoryRegistry.get(search_path=temp_dir)
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

    factory = UiPathRuntimeFactoryRegistry.get(search_path=temp_dir)
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

    factory = UiPathRuntimeFactoryRegistry.get(name="langgraph")
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
