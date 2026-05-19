# Runtime Wrapper Extension Point

This document describes the runtime wrapper extension point added to the UiPath Runtime SDK, enabling governance, compliance, and other cross-cutting concerns to be transparently injected into agent runtimes.

## Overview

The runtime wrapper extension point allows third-party packages to register wrapper functions that are automatically applied to all agent runtimes. This enables:

- **Governance/Compliance**: Policy evaluation at agent lifecycle hooks
- **Observability**: Custom tracing and metrics
- **Security**: Audit logging and access control
- **Rate Limiting**: Request throttling and quota management

## Architecture

### Components

```
┌─────────────────────────────────────────────────────────────────┐
│                    UiPathRuntimeFactoryRegistry                 │
│                              │                                  │
│                              ▼                                  │
│                 UiPathWrappedRuntimeFactory                     │
│                    (auto-wraps factories)                       │
│                              │                                  │
│                              ▼                                  │
│                 UiPathRuntimeWrapperRegistry                    │
│                   (discovers & applies wrappers)                │
│                              │                                  │
│              ┌───────────────┼───────────────┐                  │
│              ▼               ▼               ▼                  │
│         Wrapper 1       Wrapper 2       Wrapper N               │
│        (priority=100)  (priority=200)  (priority=N)             │
└─────────────────────────────────────────────────────────────────┘
```

### Files Added/Modified

| File | Description |
|------|-------------|
| `src/uipath/runtime/wrapper.py` | New - Wrapper registry and protocol |
| `src/uipath/runtime/registry.py` | Modified - Added `UiPathWrappedRuntimeFactory` |
| `src/uipath/runtime/__init__.py` | Modified - Export new classes |

## API Reference

### UiPathRuntimeWrapperRegistry

Global registry for runtime wrappers.

```python
from uipath.runtime import UiPathRuntimeWrapperRegistry

# Register a wrapper
UiPathRuntimeWrapperRegistry.register(
    name="my-wrapper",
    wrapper=my_wrapper_function,
    priority=100,  # Lower = applied first
)

# Check registered wrappers
wrappers = UiPathRuntimeWrapperRegistry.get_registered()
# Returns: {'my-wrapper': 100, 'compliance': 100}

# Check if a wrapper is registered
is_registered = UiPathRuntimeWrapperRegistry.is_registered("compliance")

# Unregister a wrapper
UiPathRuntimeWrapperRegistry.unregister("my-wrapper")

# Clear all wrappers (for testing)
UiPathRuntimeWrapperRegistry.clear()
```

### Wrapper Function Signature

```python
from uipath.runtime import UiPathRuntimeProtocol, UiPathRuntimeContext

def my_wrapper(
    runtime: UiPathRuntimeProtocol,
    context: UiPathRuntimeContext | None,
    runtime_id: str,
) -> UiPathRuntimeProtocol:
    """Wrap a runtime with additional functionality.

    Args:
        runtime: The runtime to wrap
        context: Runtime context (may be None)
        runtime_id: Unique identifier for this runtime instance

    Returns:
        Wrapped runtime implementing UiPathRuntimeProtocol
    """
    return MyWrappedRuntime(runtime, context, runtime_id)
```

### Priority System

Wrappers are applied in priority order (lowest first):

| Priority | Use Case |
|----------|----------|
| 100 | Governance/Compliance (innermost) |
| 200 | Observability/Tracing |
| 300 | Security/Audit |
| 400+ | Custom wrappers (outermost) |

Lower priority wrappers are applied first, meaning higher priority wrappers wrap around them.

## Usage

### Method 1: Entry Points (Recommended)

Register wrappers via `pyproject.toml` entry points for automatic discovery:

```toml
[project.entry-points."uipath.runtime.wrappers"]
my-governance = "my_package.governance:register_wrapper"
```

The registration function:

```python
# my_package/governance.py
from uipath.runtime import UiPathRuntimeWrapperRegistry

def my_wrapper(runtime, context, runtime_id):
    return GovernanceRuntime(runtime, context, runtime_id)

def register_wrapper():
    UiPathRuntimeWrapperRegistry.register(
        name="governance",
        wrapper=my_wrapper,
        priority=100,
    )
```

### Method 2: Programmatic Registration

Register wrappers directly in code:

```python
from uipath.runtime import UiPathRuntimeWrapperRegistry

def my_wrapper(runtime, context, runtime_id):
    return MyWrappedRuntime(runtime, context, runtime_id)

# Register at module load or application startup
UiPathRuntimeWrapperRegistry.register(
    name="my-wrapper",
    wrapper=my_wrapper,
    priority=150,
)
```

## Implementation Details

### UiPathWrappedRuntimeFactory

The `UiPathRuntimeFactoryRegistry.get()` method now returns a `UiPathWrappedRuntimeFactory` that automatically applies registered wrappers:

```python
class UiPathWrappedRuntimeFactory:
    async def new_runtime(self, entrypoint, runtime_id, **kwargs):
        # Create base runtime from delegate factory
        runtime = await self._delegate.new_runtime(entrypoint, runtime_id, **kwargs)

        # Apply all registered wrappers
        return UiPathRuntimeWrapperRegistry.wrap_runtime(
            runtime, self._context, runtime_id
        )
```

### Entry Point Discovery

Entry points are loaded lazily on first access:

```python
# Entry point group
WRAPPER_ENTRY_POINT_GROUP = "uipath.runtime.wrappers"

# Discovery happens automatically when:
# 1. UiPathRuntimeWrapperRegistry.wrap_runtime() is called
# 2. UiPathRuntimeWrapperRegistry.get_registered() is called
# 3. UiPathRuntimeWrapperRegistry.load_entry_points() is called explicitly
```

## Example: Compliance Wrapper

A complete example of a compliance wrapper:

```python
from uipath.runtime import (
    UiPathRuntimeProtocol,
    UiPathRuntimeContext,
    UiPathRuntimeWrapperRegistry,
)

class ComplianceRuntime:
    """Runtime wrapper that evaluates compliance rules."""

    def __init__(
        self,
        delegate: UiPathRuntimeProtocol,
        context: UiPathRuntimeContext | None,
        runtime_id: str,
    ):
        self._delegate = delegate
        self._context = context
        self._runtime_id = runtime_id

    async def execute(self, input, options=None):
        # Pre-execution compliance check
        self._evaluate_before_execution(input)

        # Execute delegate
        result = await self._delegate.execute(input, options)

        # Post-execution compliance check
        self._evaluate_after_execution(result)

        return result

    async def stream(self, input, options=None):
        self._evaluate_before_execution(input)
        async for event in self._delegate.stream(input, options):
            yield event

    async def get_schema(self):
        return await self._delegate.get_schema()

    async def dispose(self):
        await self._delegate.dispose()


def compliance_wrapper(runtime, context, runtime_id):
    return ComplianceRuntime(runtime, context, runtime_id)


def register_compliance_wrapper():
    UiPathRuntimeWrapperRegistry.register(
        name="compliance",
        wrapper=compliance_wrapper,
        priority=100,
    )
```

## Testing

### Unit Testing Wrappers

```python
import pytest
from uipath.runtime import UiPathRuntimeWrapperRegistry

@pytest.fixture(autouse=True)
def clear_registry():
    """Clear registry before each test."""
    UiPathRuntimeWrapperRegistry.clear()
    yield
    UiPathRuntimeWrapperRegistry.clear()

def test_wrapper_registration():
    def my_wrapper(runtime, context, runtime_id):
        return runtime

    UiPathRuntimeWrapperRegistry.register("test", my_wrapper, priority=100)

    assert UiPathRuntimeWrapperRegistry.is_registered("test")
    assert UiPathRuntimeWrapperRegistry.get_registered() == {"test": 100}

def test_wrapper_priority_order():
    applied = []

    def wrapper_a(runtime, context, runtime_id):
        applied.append("a")
        return runtime

    def wrapper_b(runtime, context, runtime_id):
        applied.append("b")
        return runtime

    UiPathRuntimeWrapperRegistry.register("b", wrapper_b, priority=200)
    UiPathRuntimeWrapperRegistry.register("a", wrapper_a, priority=100)

    mock_runtime = MockRuntime()
    UiPathRuntimeWrapperRegistry.wrap_runtime(mock_runtime, None, "test-id")

    # Lower priority applied first
    assert applied == ["a", "b"]
```

## Migration Guide

### From Old SDK (No Wrapper Support)

If using an older SDK version without `UiPathRuntimeWrapperRegistry`, the wrapper registration will fail gracefully and fall back to alternative mechanisms (see agents package documentation).

### Checking SDK Support

```python
def has_sdk_wrapper_support() -> bool:
    try:
        from uipath.runtime import UiPathRuntimeWrapperRegistry
        return True
    except ImportError:
        return False
```
