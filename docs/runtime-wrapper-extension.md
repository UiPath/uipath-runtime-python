# Governance Integration Point

`uipath-runtime` wraps runtimes with governance via a single direct
function, `apply_governance_wrapper`, gated by the
`EnablePythonGovernanceChecker` feature flag.

`uipath-governance` is a hard dependency of `uipath-runtime`. When the
flag is off, the package is **not imported** — its transitive cost
stays off the startup path. If a second wrapper is ever needed,
reintroduce a registry at that point.

## How it works

```
UiPathRuntimeFactoryRegistry.get(...)
    ↓ returns
UiPathWrappedRuntimeFactory.new_runtime(...)
    ↓ calls
apply_governance_wrapper(runtime, context, runtime_id)
    ↓
    if _is_governance_enabled():
        from uipath.governance.wrapper import governance_wrapper  # lazy
        return governance_wrapper(runtime, context, runtime_id)
    else:
        return runtime  # unwrapped, no governance import
```

## Feature flag

| Setting | Effect |
|---|---|
| `FeatureFlags.configure_flags({"EnablePythonGovernanceChecker": True})` (typically via gitops) | Governance is applied |
| `UIPATH_FEATURE_EnablePythonGovernanceChecker=true` env var | Governance is applied (fallback when no programmatic config) |
| Neither set | Governance **not** applied; `uipath-governance` is **not imported** |

Resolution and fallback semantics come from `uipath-core`'s
`FeatureFlags.is_flag_enabled(..., default=False)`. Programmatic
configuration beats env var.

## API

```python
from uipath.runtime import (
    GOVERNANCE_FEATURE_FLAG,    # "EnablePythonGovernanceChecker"
    apply_governance_wrapper,   # the call-site
)
```

`apply_governance_wrapper(runtime, context, runtime_id)` is an
`async` function. It returns the original runtime untouched when the
flag is off or when the wrapper itself raises — governance failures
must never break agent execution.

## Why deferred-import matters

When the flag is off, `apply_governance_wrapper` returns before the
`from uipath.governance.wrapper import governance_wrapper` line ever
runs. That keeps `uipath-governance`'s transitive imports — audit,
evaluator, traces, OpenTelemetry, the compiled policy index — entirely
off the startup hot path.

## Testing

Force the flag on/off per test via `FeatureFlags`:

```python
from uipath.core.feature_flags import FeatureFlags
from uipath.runtime.wrapper import GOVERNANCE_FEATURE_FLAG

# Force enable
FeatureFlags.configure_flags({GOVERNANCE_FEATURE_FLAG: True})

# Force disable
FeatureFlags.configure_flags({GOVERNANCE_FEATURE_FLAG: False})

# Reset (typically in a teardown fixture)
FeatureFlags.reset_flags()
```

Use `sys.modules` patching to stub `uipath.governance.wrapper` when you
need to assert against the wrapper invocation without actually
importing the governance package — see `tests/test_wrapper.py` for the
fixture.

## Migration from the old registry pattern

| Old | New |
|---|---|
| `from uipath.runtime import runtime_wrapper_registry` | Gone |
| `runtime_wrapper_registry.register("governance", governance_wrapper, ...)` | Gone — no registration step |
| `[project.entry-points."uipath.runtime.wrappers"] governance = "..."` in `uipath-governance/pyproject.toml` | Removed |
| `runtime_wrapper_registry.wrap_runtime(runtime, ...)` | `apply_governance_wrapper(runtime, ...)` |
| `ALLOWED_WRAPPERS` / `ALLOWED_WRAPPER_PACKAGES` allowlists | Gone — direct import is its own allowlist (only `uipath-governance` is referenced) |
