"""Tests for :class:`UiPathGovernedRuntime` — pure resolved-policy wrapper.

The runtime takes an already-resolved :class:`PolicyIndex` +
:class:`EnforcementMode` at construction (the host fetched the policy
asynchronously via the :class:`GovernancePolicyProvider` and compiled
the YAML). Tests here confirm the wrapper holds the snapshot and
passes execution straight through to the delegate.
"""

from __future__ import annotations

from typing import Any

import pytest
from uipath.core.governance import EnforcementMode

from uipath.runtime.governance.native import (
    build_policy_index_from_yaml,
)
from uipath.runtime.governance.native.models import PolicyIndex
from uipath.runtime.governance.runtime import UiPathGovernedRuntime

SIMPLE_POLICY_YAML = """
standard: provider-pack
version: "1.0"
rules:
  - id: r1
    hook: before_model
    checks:
      - type: regex
        patterns: ["leak"]
"""


# ---------------------------------------------------------------------------
# build_policy_index_from_yaml — host-side compile path
# ---------------------------------------------------------------------------


def test_build_policy_index_from_yaml_compiles_pack() -> None:
    """The host uses this to turn the provider's YAML response into the snapshot."""
    index = build_policy_index_from_yaml(SIMPLE_POLICY_YAML)
    assert isinstance(index, PolicyIndex)
    assert index.total_rules == 1
    assert "provider-pack" in index.pack_names


def test_build_policy_index_from_yaml_empty_yields_empty_index() -> None:
    """Empty YAML compiles to an empty PolicyIndex — host can pass straight through."""
    index = build_policy_index_from_yaml("")
    assert isinstance(index, PolicyIndex)
    assert index.total_rules == 0


# ---------------------------------------------------------------------------
# UiPathGovernedRuntime — passthroughs
# ---------------------------------------------------------------------------


class _StubDelegate:
    """Captures delegate calls so the passthroughs can be asserted."""

    def __init__(self) -> None:
        self.execute_calls: list[tuple[Any, Any]] = []
        self.stream_calls: list[tuple[Any, Any]] = []
        self.disposed = False
        self.schema_called = False

    async def execute(self, input: Any = None, options: Any = None) -> Any:
        self.execute_calls.append((input, options))
        return "result"

    async def stream(self, input: Any = None, options: Any = None) -> Any:
        self.stream_calls.append((input, options))
        for event in ("a", "b"):
            yield event

    async def get_schema(self) -> Any:
        self.schema_called = True
        return "schema"

    async def dispose(self) -> None:
        self.disposed = True


def _make_runtime(
    delegate: _StubDelegate | None = None,
    *,
    policy_index: PolicyIndex | None = None,
    enforcement_mode: EnforcementMode = EnforcementMode.AUDIT,
) -> UiPathGovernedRuntime:
    """Build a runtime with sensible test defaults."""
    return UiPathGovernedRuntime(
        delegate or _StubDelegate(),
        policy_index if policy_index is not None else PolicyIndex(),
        enforcement_mode,
    )


# ---------------------------------------------------------------------------
# Snapshot stored internally — not exposed as a public property
# ---------------------------------------------------------------------------


def test_resolved_policy_index_is_held_for_evaluator_use() -> None:
    """The wrapper stores the resolved snapshot; the evaluator reads it."""
    index = build_policy_index_from_yaml(SIMPLE_POLICY_YAML)
    runtime = _make_runtime(policy_index=index)
    # Internal attribute — verify the wrapper kept the exact instance.
    assert runtime._policy_index is index


def test_enforcement_mode_is_held_for_evaluator_use() -> None:
    """The wrapper stores the mode supplied at construction."""
    runtime = _make_runtime(enforcement_mode=EnforcementMode.ENFORCE)
    assert runtime._enforcement_mode is EnforcementMode.ENFORCE


def test_empty_policy_index_is_a_valid_construction() -> None:
    """``PolicyIndex()`` with no packs is acceptable — wrapper attaches without rules."""
    runtime = _make_runtime(policy_index=PolicyIndex())
    assert runtime._policy_index.total_rules == 0


# ---------------------------------------------------------------------------
# Passthrough behavior
# ---------------------------------------------------------------------------


async def test_governance_runtime_execute_delegates() -> None:
    delegate = _StubDelegate()
    runtime = _make_runtime(delegate)

    result = await runtime.execute({"x": 1})

    assert result == "result"
    assert delegate.execute_calls == [({"x": 1}, None)]


async def test_governance_runtime_stream_delegates() -> None:
    delegate = _StubDelegate()
    runtime = _make_runtime(delegate)

    events = [e async for e in runtime.stream({"x": 1})]

    assert events == ["a", "b"]
    assert delegate.stream_calls == [({"x": 1}, None)]


async def test_governance_runtime_schema_and_dispose_delegate() -> None:
    delegate = _StubDelegate()
    runtime = _make_runtime(delegate)

    assert await runtime.get_schema() == "schema"
    await runtime.dispose()
    assert delegate.schema_called
    assert delegate.disposed


# ---------------------------------------------------------------------------
# on_dispose — host-owned cleanup hook
# ---------------------------------------------------------------------------


async def test_dispose_runs_on_dispose_after_delegate() -> None:
    """Ordering — the delegate is disposed first, then the host's hook runs.

    Rationale: teardown of the wrapped runtime is the primary concern; the
    host's hook is auxiliary cleanup that gets to observe the post-dispose
    state (or that has independent resources like a telemetry dispatcher's
    thread pool to flush).
    """
    delegate = _StubDelegate()
    order: list[str] = []

    def _hook() -> None:
        order.append("on_dispose")

    # Wrap the delegate's dispose so we can observe ordering without
    # depending on side-effect timing.
    _orig_dispose = delegate.dispose

    async def _tracked_dispose() -> None:
        await _orig_dispose()
        order.append("delegate.dispose")

    delegate.dispose = _tracked_dispose  # type: ignore[method-assign]

    runtime = UiPathGovernedRuntime(
        delegate, PolicyIndex(), EnforcementMode.AUDIT, on_dispose=_hook
    )

    await runtime.dispose()

    assert order == ["delegate.dispose", "on_dispose"]


async def test_dispose_runs_on_dispose_even_when_delegate_raises() -> None:
    """``finally`` semantics — a broken delegate must not skip host cleanup.

    Regression guard: without ``try/finally``, a raise from the delegate
    would leak past ``dispose`` and never call ``on_dispose``, leaving
    the host's dispatcher (or any other resource) un-flushed.
    """
    hook_called = False

    def _hook() -> None:
        nonlocal hook_called
        hook_called = True

    class _RaisingDelegate(_StubDelegate):
        async def dispose(self) -> None:  # type: ignore[override]
            raise RuntimeError("delegate boom")

    runtime = UiPathGovernedRuntime(
        _RaisingDelegate(), PolicyIndex(), EnforcementMode.AUDIT, on_dispose=_hook
    )

    with pytest.raises(RuntimeError, match="delegate boom"):
        await runtime.dispose()

    assert hook_called, (
        "on_dispose must run even when the delegate's dispose raises — "
        "regression guard for the try/finally contract"
    )


async def test_dispose_is_a_noop_without_on_dispose() -> None:
    """Default (``on_dispose=None``) leaves ``dispose`` semantically identical
    to the passthrough case — no crash, no extra work.
    """
    delegate = _StubDelegate()
    runtime = _make_runtime(delegate)  # on_dispose defaults to None

    await runtime.dispose()

    assert delegate.disposed
    # No assertion on the hook — the point is that its absence
    # doesn't affect anything.


# ---------------------------------------------------------------------------
# Root-span trace correlation — one trace_id per agent run
# ---------------------------------------------------------------------------


async def test_execute_opens_a_root_span_that_covers_the_whole_invocation() -> None:
    """A single OTel span wraps BEFORE_AGENT, delegate, and AFTER_AGENT.

    Verifies the unified-trace contract: every code path under
    ``execute`` — including any framework adapter span the delegate
    opens — sees the same ``trace_id`` and the same current span. The
    ``track_events`` sink reads ``trace.get_current_span()`` at emit
    time, so this guarantee is what makes its ``operation_id``
    correlate across BEFORE_AGENT, BEFORE_MODEL, AFTER_MODEL, and
    AFTER_AGENT events.
    """
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    # Replace the global tracer provider for this test so our span
    # actually records (the default NoOp provider returns invalid
    # contexts).
    trace.set_tracer_provider(TracerProvider())

    seen_trace_ids: list[int] = []

    class _SpanProbeDelegate:
        async def execute(self, input: Any = None, options: Any = None) -> Any:
            seen_trace_ids.append(
                trace.get_current_span().get_span_context().trace_id
            )
            return "result"

        async def stream(self, input: Any = None, options: Any = None) -> Any:
            if False:
                yield  # pragma: no cover

        async def get_schema(self) -> Any:
            return "schema"

        async def dispose(self) -> None:
            return None

    runtime = UiPathGovernedRuntime(
        _SpanProbeDelegate(),
        PolicyIndex(),
        EnforcementMode.AUDIT,
        agent_name="agent-x",
        runtime_id="run-1",
    )

    # Outside execute(): no governance span is active.
    pre_call_ctx = trace.get_current_span().get_span_context()
    assert not pre_call_ctx.is_valid, (
        "no span should be active before execute() opens the wrapper"
    )

    await runtime.execute({"x": 1})

    # Inside the delegate: a valid span context was current.
    assert len(seen_trace_ids) == 1
    assert seen_trace_ids[0] != 0, (
        "delegate must see a valid OTel trace_id from the wrapping span"
    )

    # After execute(): the span has closed.
    post_call_ctx = trace.get_current_span().get_span_context()
    assert not post_call_ctx.is_valid, "wrapper span must close on return"


async def test_execute_span_inherits_host_trace_id_when_one_is_active() -> None:
    """When the host opens a span first, our wrapper becomes a child.

    Same ``trace_id`` flows through everything — governance events
    emitted by runtime hooks correlate with the host's outer
    operation. The host's OTel context is preserved end-to-end.
    """
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    trace.set_tracer_provider(TracerProvider())
    tracer = trace.get_tracer("test-host")

    inner_trace_id: list[int] = []

    class _ProbeDelegate:
        async def execute(self, input: Any = None, options: Any = None) -> Any:
            inner_trace_id.append(
                trace.get_current_span().get_span_context().trace_id
            )
            return "result"

        async def stream(self, input: Any = None, options: Any = None) -> Any:
            if False:
                yield  # pragma: no cover

        async def get_schema(self) -> Any:
            return "schema"

        async def dispose(self) -> None:
            return None

    runtime = UiPathGovernedRuntime(
        _ProbeDelegate(), PolicyIndex(), EnforcementMode.AUDIT
    )

    with tracer.start_as_current_span("host-outer") as host_span:
        host_trace_id = host_span.get_span_context().trace_id
        await runtime.execute({"x": 1})

    # The trace_id seen inside the delegate equals the host's
    # trace_id — the wrapper became a child of the host's span.
    assert inner_trace_id == [host_trace_id]


async def test_stream_opens_a_root_span_that_covers_iteration() -> None:
    """``stream``'s span stays open across the entire ``async for``.

    Holding the span over ``await`` boundaries is safe — Python's
    asyncio propagates contextvars (including OTel's current-span
    pointer) across suspensions. The delegate (which yields events)
    must see the same wrapper span on every iteration.
    """
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    trace.set_tracer_provider(TracerProvider())

    per_event_trace_ids: list[int] = []

    class _StreamProbeDelegate:
        async def execute(self, input: Any = None, options: Any = None) -> Any:
            return "result"

        async def stream(self, input: Any = None, options: Any = None) -> Any:
            for label in ("a", "b", "c"):
                per_event_trace_ids.append(
                    trace.get_current_span().get_span_context().trace_id
                )
                yield label

        async def get_schema(self) -> Any:
            return "schema"

        async def dispose(self) -> None:
            return None

    runtime = UiPathGovernedRuntime(
        _StreamProbeDelegate(), PolicyIndex(), EnforcementMode.AUDIT
    )

    events = [e async for e in runtime.stream({"x": 1})]

    assert events == ["a", "b", "c"]
    # Same valid trace_id on every yield — one span covers the whole
    # stream.
    assert len(set(per_event_trace_ids)) == 1
    assert per_event_trace_ids[0] != 0
