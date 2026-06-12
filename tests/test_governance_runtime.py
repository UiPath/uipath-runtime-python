"""Tests for :class:`UiPathGovernedRuntime` — pure resolved-policy wrapper.

The runtime takes an already-resolved :class:`PolicyIndex` +
:class:`EnforcementMode` at construction. The caller resolves and
compiles the policy on its own side; the runtime is format-agnostic
and never sees a wire representation. Tests here confirm the wrapper
holds the snapshot and passes execution straight through to the
delegate.
"""

from __future__ import annotations

from typing import Any

import pytest
from uipath.core.governance import EnforcementMode

from uipath.runtime.governance.native.models import PolicyIndex
from uipath.runtime.governance.runtime import UiPathGovernedRuntime

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
    """The wrapper stores the resolved snapshot; the evaluator reads it.

    The wrapper is oblivious to how the index was built (any wire
    format, any compilation path, or a hand-constructed test fixture)
    — it just holds the instance. A plain ``PolicyIndex()`` suffices
    to prove the reference is preserved.
    """
    index = PolicyIndex()
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


async def test_governance_runtime_schema_delegates() -> None:
    """``get_schema`` still forwards — only ``dispose`` was cut to a no-op."""
    delegate = _StubDelegate()
    runtime = _make_runtime(delegate)

    assert await runtime.get_schema() == "schema"
    assert delegate.schema_called


# ---------------------------------------------------------------------------
# dispose — no-op by contract; caller owns delegate + host cleanup
# ---------------------------------------------------------------------------


async def test_dispose_does_not_touch_the_delegate() -> None:
    """The wrapper deliberately does not forward ``dispose`` to the delegate.

    Caller-owned lifecycle: whichever code built the delegate is
    responsible for disposing it, along with any adjacent resources
    the caller wired up (telemetry dispatchers, session pools, etc.).
    The wrapper's ``dispose`` exists only to satisfy
    :class:`UiPathDisposableProtocol` so the wrapper stays structurally
    substitutable for any runtime.
    """
    delegate = _StubDelegate()
    runtime = _make_runtime(delegate)

    await runtime.dispose()

    # The delegate was never touched — caller must dispose it themselves.
    assert delegate.disposed is False


async def test_dispose_does_not_raise_when_delegate_would_have_raised() -> None:
    """Because the wrapper's ``dispose`` never invokes the delegate,
    a delegate whose ``dispose`` raises has no path to surface through
    the wrapper.
    """

    class _RaisingDelegate(_StubDelegate):
        async def dispose(self) -> None:
            raise RuntimeError("delegate would boom — but wrapper never calls this")

    runtime = _make_runtime(_RaisingDelegate())

    # Must not raise — the wrapper's dispose is a plain no-op.
    await runtime.dispose()


async def test_dispose_is_idempotent() -> None:
    """Two calls in a row must not raise — teardown paths sometimes wire
    ``close``/``dispose`` from multiple layers.
    """
    runtime = _make_runtime()

    await runtime.dispose()
    await runtime.dispose()  # second call: no crash, no state to corrupt


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
            seen_trace_ids.append(trace.get_current_span().get_span_context().trace_id)
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
            inner_trace_id.append(trace.get_current_span().get_span_context().trace_id)
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


# ---------------------------------------------------------------------------
# _serialize_payload — evaluator input/output flattening
# ---------------------------------------------------------------------------


def test_serialize_payload_none_returns_empty_string() -> None:
    """``None`` becomes ``""`` — the evaluator's regex/sentiment checks
    scan a string and would break on a raw ``None``.
    """
    from uipath.runtime.governance.runtime import _serialize_payload

    assert _serialize_payload(None) == ""


def test_serialize_payload_string_passes_through_unchanged() -> None:
    """A bare string must not be JSON-quoted — regex/sentiment expect the
    text as-is, without surrounding double quotes.
    """
    from uipath.runtime.governance.runtime import _serialize_payload

    assert _serialize_payload("hello world") == "hello world"


def test_serialize_payload_dict_is_json_encoded() -> None:
    """Structured payloads route through ``serialize_object`` then
    ``json.dumps`` — the evaluator sees a flat JSON string it can scan.
    """
    from uipath.runtime.governance.runtime import _serialize_payload

    result = _serialize_payload({"a": 1, "b": ["x", "y"]})
    # Order-agnostic content check — json.dumps default ordering can vary
    # by dict insertion order across Python versions.
    assert '"a": 1' in result
    assert '"b": ["x", "y"]' in result


def test_serialize_payload_unserializable_object_falls_back_to_str() -> None:
    """When ``serialize_object`` + ``json.dumps`` can't handle the value
    (e.g., raw file handles, sockets, generators), the wrapper falls back
    to ``str(payload)`` so the evaluator still gets *something* rather
    than crashing the agent hook.
    """
    from uipath.runtime.governance.runtime import _serialize_payload

    class _Unpicklable:
        def __repr__(self) -> str:
            return "<unpicklable-repr>"

    # A recursive dict that can't JSON-encode without hitting cycles or
    # non-serializable values.
    obj = _Unpicklable()
    result = _serialize_payload(obj)
    assert result == "<unpicklable-repr>"


# ---------------------------------------------------------------------------
# _fire_before_agent / _fire_after_agent — evaluator-wired paths
# ---------------------------------------------------------------------------


class _CapturingEvaluator:
    """Minimal stub for :class:`GovernanceEvaluator` — records calls.

    The runtime only invokes ``evaluate_before_agent`` /
    ``evaluate_after_agent`` on the evaluator, so those two are the only
    methods this stub needs to expose.
    """

    def __init__(self) -> None:
        self.before_calls: list[dict[str, Any]] = []
        self.after_calls: list[dict[str, Any]] = []
        self.before_raises: Exception | None = None
        self.after_raises: Exception | None = None

    def evaluate_before_agent(self, **kwargs: Any) -> None:
        self.before_calls.append(kwargs)
        if self.before_raises is not None:
            raise self.before_raises

    def evaluate_after_agent(self, **kwargs: Any) -> None:
        self.after_calls.append(kwargs)
        if self.after_raises is not None:
            raise self.after_raises


class _ResultDelegate:
    """Delegate that returns a real ``UiPathRuntimeResult`` so
    :meth:`_fire_after_agent` can read ``result.output`` without
    tripping over the ``_StubDelegate``'s bare-string return.
    """

    def __init__(self) -> None:
        self.execute_calls: list[tuple[Any, Any]] = []

    async def execute(self, input: Any = None, options: Any = None) -> Any:
        from uipath.runtime.result import UiPathRuntimeResult, UiPathRuntimeStatus

        self.execute_calls.append((input, options))
        return UiPathRuntimeResult(
            output="agent-output", status=UiPathRuntimeStatus.SUCCESSFUL
        )

    async def stream(self, input: Any = None, options: Any = None) -> Any:
        if False:
            yield  # pragma: no cover

    async def get_schema(self) -> Any:
        return "schema"

    async def dispose(self) -> None:
        return None


async def test_execute_fires_before_and_after_agent_when_evaluator_wired() -> None:
    """The evaluator's hooks fire on happy-path execute — this is the
    core reason ``UiPathGovernedRuntime`` exists.

    Regression guard: without wiring the evaluator's calls, the wrapper
    silently degrades to a pure passthrough (the ``self._evaluator is
    None`` early-returns in ``_fire_*`` would kick in), and no
    governance would run.
    """
    delegate = _ResultDelegate()
    evaluator = _CapturingEvaluator()
    runtime = UiPathGovernedRuntime(
        delegate,
        PolicyIndex(),
        EnforcementMode.AUDIT,
        evaluator=evaluator,  # type: ignore[arg-type]
        agent_name="agent-x",
        runtime_id="run-1",
    )

    result = await runtime.execute({"prompt": "leak?"})

    assert result.output == "agent-output"
    assert len(evaluator.before_calls) == 1
    assert len(evaluator.after_calls) == 1
    # ``_serialize_payload`` turned the dict input into a JSON string.
    assert "leak" in evaluator.before_calls[0]["agent_input"]
    assert evaluator.before_calls[0]["agent_name"] == "agent-x"
    assert evaluator.before_calls[0]["runtime_id"] == "run-1"
    # And AFTER_AGENT saw the delegate's output.
    assert evaluator.after_calls[0]["agent_output"] == "agent-output"


async def test_execute_propagates_governance_block_exception() -> None:
    """A DENY in ``ENFORCE`` mode must halt the run — the evaluator
    raises :class:`GovernanceBlockException` and the wrapper propagates
    it rather than swallowing.
    """
    from uipath.core.governance.exceptions import GovernanceBlockException

    evaluator = _CapturingEvaluator()
    evaluator.before_raises = GovernanceBlockException("policy denied")

    runtime = UiPathGovernedRuntime(
        _StubDelegate(),
        PolicyIndex(),
        EnforcementMode.ENFORCE,
        evaluator=evaluator,  # type: ignore[arg-type]
    )

    with pytest.raises(GovernanceBlockException):
        await runtime.execute({"x": 1})


async def test_after_agent_block_exception_also_propagates() -> None:
    """The re-raise contract holds for AFTER_AGENT too, not just
    BEFORE_AGENT.

    Even though DENY on output is a rarer configuration (most policies
    fire pre-model / on tool call), the wrapper must treat both hooks
    symmetrically — a governance block from either boundary halts the
    run, not just the input side.
    """
    from uipath.core.governance.exceptions import GovernanceBlockException

    evaluator = _CapturingEvaluator()
    evaluator.after_raises = GovernanceBlockException("output policy denied")

    runtime = UiPathGovernedRuntime(
        _ResultDelegate(),
        PolicyIndex(),
        EnforcementMode.ENFORCE,
        evaluator=evaluator,  # type: ignore[arg-type]
    )

    with pytest.raises(GovernanceBlockException):
        await runtime.execute({"x": 1})


async def test_execute_swallows_unexpected_evaluator_errors() -> None:
    """A generic ``Exception`` from the evaluator must NOT break the
    agent run — governance bugs are logged and the delegate still runs.
    """
    delegate = _StubDelegate()
    evaluator = _CapturingEvaluator()
    evaluator.before_raises = RuntimeError("evaluator internal error")

    runtime = UiPathGovernedRuntime(
        delegate,
        PolicyIndex(),
        EnforcementMode.AUDIT,
        evaluator=evaluator,  # type: ignore[arg-type]
    )

    # Must not raise — the wrapper swallows non-block exceptions so a
    # governance bug never breaks the agent.
    result = await runtime.execute({"x": 1})
    assert result == "result"
    # The delegate still ran (the evaluator failure didn't short-circuit).
    assert delegate.execute_calls == [({"x": 1}, None)]


async def test_stream_fires_after_agent_only_on_result_event() -> None:
    """AFTER_AGENT fires when a :class:`UiPathRuntimeResult` event is
    yielded — intermediate events pass through untouched.

    Uses a delegate that yields a mix of plain events + a
    ``UiPathRuntimeResult`` sentinel so the branch is exercised.
    """
    from uipath.runtime.result import UiPathRuntimeResult, UiPathRuntimeStatus

    class _MixedStreamDelegate:
        async def execute(self, input: Any = None, options: Any = None) -> Any:
            return None

        async def stream(self, input: Any = None, options: Any = None) -> Any:
            yield "intermediate-1"
            yield UiPathRuntimeResult(
                output="final-output", status=UiPathRuntimeStatus.SUCCESSFUL
            )
            yield "intermediate-2"

        async def get_schema(self) -> Any:
            return "schema"

        async def dispose(self) -> None:
            return None

    evaluator = _CapturingEvaluator()
    runtime = UiPathGovernedRuntime(
        _MixedStreamDelegate(),
        PolicyIndex(),
        EnforcementMode.AUDIT,
        evaluator=evaluator,  # type: ignore[arg-type]
    )

    events = [e async for e in runtime.stream({"x": 1})]

    # All three events came through in order.
    assert len(events) == 3
    assert isinstance(events[1], UiPathRuntimeResult)
    # AFTER_AGENT fires exactly once — on the UiPathRuntimeResult.
    assert len(evaluator.after_calls) == 1
    # BEFORE_AGENT fires once at stream start.
    assert len(evaluator.before_calls) == 1


# ---------------------------------------------------------------------------
# OTel-not-installed fallback for _governance_root_span
# ---------------------------------------------------------------------------


async def test_execute_still_runs_when_opentelemetry_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runtime must not depend on OTel being importable.

    Simulate a stripped host by making ``from opentelemetry import trace``
    inside ``_governance_root_span`` raise ``ImportError``. The wrapper
    should yield without opening a span and the agent still runs.
    """
    import builtins

    real_import = builtins.__import__

    def _blocked_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "opentelemetry" or name.startswith("opentelemetry."):
            raise ImportError(f"simulated missing: {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)

    delegate = _StubDelegate()
    runtime = UiPathGovernedRuntime(delegate, PolicyIndex(), EnforcementMode.AUDIT)

    # Must not raise. The context manager takes the ImportError branch,
    # yields with no span, and execute completes normally.
    result = await runtime.execute({"x": 1})

    assert result == "result"
    assert delegate.execute_calls == [({"x": 1}, None)]
