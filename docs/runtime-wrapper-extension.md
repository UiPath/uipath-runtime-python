# Runtime Wrapper Extension Point

This document describes the runtime wrapper extension point in the UiPath Runtime SDK, enabling governance to be transparently injected into agent runtimes.

## Overview

The runtime wrapper extension point allows the `uipath-governance` package to register a wrapper that is automatically applied to all agent runtimes. This enables policy evaluation at agent lifecycle hooks.

## Security Model

The wrapper system is locked down to prevent unauthorized packages from intercepting runtime executions.

### Allowed Wrappers

Only explicitly allowed wrapper names can be registered:

```python
from uipath.runtime import ALLOWED_WRAPPERS

print(ALLOWED_WRAPPERS)
# frozenset({'governance'})
```

Any attempt to register a wrapper with a different name will raise `ValueError`:

```python
registry.register("malicious", my_wrapper)
# ValueError: Wrapper 'malicious' is not allowed.
# Only these wrappers can be registered: ['governance']
```

### Allowed Packages

Only entry points from allowed packages are loaded:

```python
from uipath.runtime import ALLOWED_WRAPPER_PACKAGES

print(ALLOWED_WRAPPER_PACKAGES)
# frozenset({'uipath-governance'})
```

Entry points from other packages are blocked with a warning:

```
WARNING - Blocked wrapper entry point 'evil' from package 'malicious-pkg':
          not in allowed packages list
```

### Why Entry Points?

The entry point pattern avoids circular dependencies:

```
uipath-runtime (defines extension point)
    ↑
    │ depends on (one-way)
    │
uipath-governance (registers via entry point)
```

If `uipath-runtime` imported `uipath-governance` directly, it would create a circular dependency since `uipath-governance` imports types from `uipath-runtime`.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    UiPathRuntimeFactoryRegistry                 │
│                              │                                  │
│                              ▼                                  │
│                 UiPathWrappedRuntimeFactory                     │
│                    (auto-wraps factories)                       │
│                              │                                  │
│                              ▼                                  │
│                 runtime_wrapper_registry                        │
│              (loads entry points, applies wrappers)             │
│                              │                                  │
│               ┌──────────────┴──────────────┐                   │
│               ▼                             ▼                   │
│      ALLOWED_WRAPPERS              ALLOWED_WRAPPER_PACKAGES     │
│     (name allowlist)                 (package allowlist)        │
│                              │                                  │
│                              ▼                                  │
│                    governance wrapper                           │
│                      (priority=100)                             │
└─────────────────────────────────────────────────────────────────┘
```

## API Reference

### Constants

```python
from uipath.runtime import (
    ALLOWED_WRAPPERS,           # frozenset({'governance'})
    ALLOWED_WRAPPER_PACKAGES,   # frozenset({'uipath-governance'})
    WRAPPER_ENTRY_POINT_GROUP,  # 'uipath.runtime.wrappers'
)
```

### runtime_wrapper_registry

```python
from uipath.runtime import runtime_wrapper_registry

# Check if governance is registered
if runtime_wrapper_registry.is_registered("governance"):
    print("Governance is active")

# Get all registered wrappers
wrappers = runtime_wrapper_registry.get_registered()
# Returns: {'governance': 100}

# Clear (for testing only)
runtime_wrapper_registry.clear()
```

### Checking Governance Status

```python
from uipath.runtime import runtime_wrapper_registry

# Force entry point loading
runtime_wrapper_registry.load_entry_points()

# Check status
if runtime_wrapper_registry.is_registered("governance"):
    print("Governance wrapper is active")
    print(f"Priority: {runtime_wrapper_registry.get_registered()['governance']}")
else:
    print("Governance wrapper not loaded")
```

## How uipath-governance Registers

The `uipath-governance` package registers via entry points:

```toml
# In uipath-governance's pyproject.toml
[project.entry-points."uipath.runtime.wrappers"]
governance = "uipath_governance:register_governance_wrapper"
```

```python
# In uipath_governance/__init__.py
def register_governance_wrapper(registry):
    """Called by uipath-runtime with the registry instance."""
    registry.register(
        name="governance",  # Must be in ALLOWED_WRAPPERS
        wrapper=governance_wrapper,
        priority=100,
    )
```

## Implementation Details

### UiPathWrappedRuntimeFactory

The factory registry returns a wrapped factory that auto-applies the governance wrapper:

```python
class UiPathWrappedRuntimeFactory(UiPathRuntimeFactoryProtocol):
    async def new_runtime(self, entrypoint, runtime_id, **kwargs):
        runtime = await self._delegate.new_runtime(entrypoint, runtime_id, **kwargs)
        return await runtime_wrapper_registry.wrap_runtime(
            runtime, self._context, runtime_id
        )
```

To bypass wrapping (for testing):

```python
factory = UiPathRuntimeFactoryRegistry.get(
    name="langgraph",
    apply_wrappers=False,
)
```

### Entry Point Loading

Entry points are loaded lazily on first access:

```python
# Triggers entry point loading:
runtime_wrapper_registry.is_registered("governance")
runtime_wrapper_registry.get_registered()
runtime_wrapper_registry.wrap_runtime(...)

# Or explicitly:
runtime_wrapper_registry.load_entry_points()
```

## Testing

### Testing Without Governance

```python
import pytest
from uipath.runtime import UiPathRuntimeFactoryRegistry

def test_without_wrappers():
    factory = UiPathRuntimeFactoryRegistry.get(
        name="langgraph",
        apply_wrappers=False,  # Skip wrapper application
    )
    # Factory is unwrapped
```

### Testing with Mock Registry

```python
import pytest
from uipath.runtime import UiPathRuntimeWrapperRegistry

@pytest.fixture
def clean_registry():
    registry = UiPathRuntimeWrapperRegistry()
    yield registry
    registry.clear()

@pytest.mark.asyncio
async def test_wrapper_application(clean_registry):
    # Register allowed wrapper
    def mock_wrapper(runtime, context, runtime_id):
        return runtime

    clean_registry.register("governance", mock_wrapper, priority=100)

    assert clean_registry.is_registered("governance")
```

## Extending the Allowlist (Internal Use Only)

To add new allowed wrappers, modify the constants in `wrapper.py`:

```python
ALLOWED_WRAPPERS: frozenset[str] = frozenset({
    "governance",
    "new-wrapper",  # Add new wrapper name
})

ALLOWED_WRAPPER_PACKAGES: frozenset[str] = frozenset({
    "uipath-governance",
    "uipath-new-package",  # Add new package
})
```

This requires a new release of `uipath-runtime`.
