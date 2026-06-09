"""SDK runtime wrapper integration with adapter-based framework support.

This module provides the wrapper function that integrates with the
UiPath Runtime SDK's UiPathRuntimeWrapperRegistry.

Architecture:
    The wrapper automatically detects and wraps agents using framework-specific
    adapters. This provides governance at all lifecycle hooks:

    - BEFORE_AGENT / AFTER_AGENT: Intercepted at runtime level in execute()/stream()
    - BEFORE_MODEL / AFTER_MODEL / TOOL_CALL: Via framework-specific adapters

    Agent Detection Flow:
    1. GovernanceRuntime receives delegate runtime from SDK
    2. Extracts agent from delegate (looks for _agent, agent, _runnable, etc.)
    3. Uses AdapterRegistry.resolve() to find matching adapter
    4. Calls adapter.attach() to wrap agent with governance hooks
    5. Replaces original agent in delegate with governed version

    Supported Frameworks (via adapters):
    - LangChain / LangGraph
    - Microsoft AutoGen
    - CrewAI
    - LlamaIndex
    - OpenAI Agents SDK
    - PydanticAI
    - Microsoft Semantic Kernel
    - HuggingFace smolagents
"""

from __future__ import annotations

import logging
import threading
from contextvars import ContextVar, Token
from typing import Any, AsyncGenerator, Dict, Optional
from uuid import uuid4

from uipath.core.adapters import get_adapter_registry
from uipath.core.governance.config import is_governance_enabled
from uipath.core.governance.exceptions import GovernanceBlockException

from uipath.runtime.base import UiPathRuntimeProtocol
from uipath.runtime.governance.config import EnforcementMode, get_enforcement_mode
from uipath.runtime.governance.delegation_guard import (
    install_delegation_guard,
    uninstall_delegation_guard,
)
from uipath.runtime.governance.native.backend_client import set_agent_conversational
from uipath.runtime.governance.native.evaluator import GovernanceEvaluator
from uipath.runtime.governance.native.loader import (
    get_policy_index,
    prefetch_policy_index,
)
from uipath.runtime.result import UiPathRuntimeResult

logger = logging.getLogger(__name__)

# Per-call-context model name set by GovernanceRuntime, read by adapters.
# A ContextVar keeps concurrent agent runs from stomping each other's
# value across threads and asyncio tasks.
_current_model_name: ContextVar[str] = ContextVar(
    "_uipath_governance_current_model_name", default=""
)


def get_current_model_name() -> str:
    """Get the model name captured from the runtime context."""
    return _current_model_name.get()


# Content keys we prioritize when walking dict-shaped agent outputs.
# These cover the common "submit final answer" / chat-message shapes that
# coded agents produce (``{"content": "..."}``, ``{"text": "..."}``, etc.)
# and the OpenAI function-call shape (``{"arguments": "<json>"}``). Any
# remaining keys are walked after these — they still contribute, just
# with lower priority so the actual answer text leads the extracted blob.
_GOVERNANCE_CONTENT_KEYS: tuple[str, ...] = (
    "content",
    "text",
    "output",
    "answer",
    "message",
    "result",
    "arguments",
    "thinking",
)

# Total cap on the extracted governance-text blob. Larger than the prior
# raw ``str(...)[:2000]`` because we're now producing clean content (no
# dict-syntax noise), so more of the budget goes toward real signal.
# Still bounded so a runaway nested payload can't blow memory or
# dominate the regex scan time.
_GOVERNANCE_TEXT_CAP = 8000

# Depth cap for the recursive walk. Anything beyond this is almost
# certainly framework plumbing, not user-facing content.
_GOVERNANCE_TEXT_MAX_DEPTH = 10


def _extract_governable_text(
    value: Any,
    *,
    budget: int = _GOVERNANCE_TEXT_CAP,
    seen: set[int] | None = None,
    depth: int = 0,
) -> str:
    """Pull governance-relevant text out of an arbitrary runtime payload.

    Replaces the prior ``str(value)[:2000]`` shortcut, which produced
    ``"{'content': '...'}"``-style garbled prefixes for structured
    outputs and ate into the budget with dict-syntax noise. Walks dicts
    (prioritising :data:`_GOVERNANCE_CONTENT_KEYS`), lists, pydantic
    models, and plain objects up to :data:`_GOVERNANCE_TEXT_MAX_DEPTH`,
    joining text fragments with newlines. Non-text scalars and unknown
    block types contribute nothing. Cycles and over-deep nesting are
    skipped silently.
    """
    if value is None or budget <= 0 or depth > _GOVERNANCE_TEXT_MAX_DEPTH:
        return ""
    if isinstance(value, str):
        return value[:budget]
    if isinstance(value, (bool, int, float)):
        # Numeric / boolean scalars aren't governance text — skip them
        # so dict walks don't pad the blob with ints / flags.
        return ""

    # Pydantic / dataclass-like shapes are easier to walk via their
    # dict form than via attribute introspection.
    for dumper in ("model_dump", "dict"):
        fn = getattr(value, dumper, None)
        if callable(fn):
            try:
                return _extract_governable_text(
                    fn(), budget=budget, seen=seen, depth=depth + 1,
                )
            except Exception:  # noqa: BLE001 - fall through to other extractors
                break

    obj_id = id(value)
    if seen is None:
        seen = set()
    if obj_id in seen:
        return ""
    if isinstance(value, dict):
        seen.add(obj_id)
        # Walk recognized content keys first so the actual reply leads
        # the extracted blob; any remaining keys follow.
        keys: list[Any] = [k for k in _GOVERNANCE_CONTENT_KEYS if k in value]
        keys.extend(k for k in value if k not in _GOVERNANCE_CONTENT_KEYS)
        parts: list[str] = []
        remaining = budget
        for key in keys:
            if remaining <= 0:
                break
            piece = _extract_governable_text(
                value[key], budget=remaining, seen=seen, depth=depth + 1,
            )
            if piece:
                parts.append(piece)
                remaining -= len(piece) + 1  # +1 accounts for the newline
        return "\n".join(parts)
    if isinstance(value, (list, tuple)):
        seen.add(obj_id)
        parts = []
        remaining = budget
        for item in value:
            if remaining <= 0:
                break
            piece = _extract_governable_text(
                item, budget=remaining, seen=seen, depth=depth + 1,
            )
            if piece:
                parts.append(piece)
                remaining -= len(piece) + 1
        return "\n".join(parts)

    # Last-resort: walk public attributes on opaque objects (e.g. a
    # framework-specific result class without model_dump/dict).
    public = {
        name: getattr(value, name)
        for name in dir(value)
        if not name.startswith("_") and not callable(getattr(value, name, None))
    }
    if public:
        return _extract_governable_text(
            public, budget=budget, seen=seen, depth=depth + 1,
        )
    return ""


class GovernanceRuntime:
    """Runtime wrapper that adds governance evaluation at the runtime level.

    Automatically detects and wraps agents using the AdapterRegistry:
    - LangChain / LangGraph
    - Microsoft AutoGen
    - CrewAI
    - LlamaIndex
    - OpenAI Agents SDK
    - PydanticAI
    - Microsoft Semantic Kernel
    - HuggingFace smolagents

    Boundary hooks (BEFORE_AGENT, AFTER_AGENT) are handled at the runtime level.
    Inner hooks (BEFORE_MODEL, TOOL_CALL, etc.) are handled by framework adapters
    which are automatically attached to the agent when the runtime is created.
    """

    # Attributes to search for agent extraction (in priority order)
    _AGENT_ATTRS = [
        "_agent",
        "agent",
        "_runnable",
        "runnable",
        "_graph",
        "graph",
        "_chain",
        "chain",
        "_crew",
        "crew",
    ]

    def __init__(
        self,
        delegate: UiPathRuntimeProtocol,
        context: Any,
        runtime_id: str,
    ) -> None:
        """Wrap ``delegate`` and prime per-run governance state.

        Captures the runtime/trace identifiers, resolves the agent name and
        model from ``context``, and kicks off the background policy fetch.
        """
        self._delegate = delegate
        self._context = context
        self._runtime_id = runtime_id
        self._trace_id = str(uuid4())
        self._governed_agent: Any = None
        self._original_agent: Any = None
        self._agent_attr_name: str | None = None
        self._agent_holder: Any = None  # The object holding the agent attribute
        self._init_failed = False  # Track if initialization failed
        self._model_name = ""
        self._model_name_token: Token[str] | None = None
        self._evaluator: GovernanceEvaluator | None = None
        self._evaluator_ready: bool = False
        self._evaluator_lock = threading.Lock()
        self._adapter_registry: Any = None

        # Agent name is needed even in the FF-off path so dispose / log
        # messages have something to print. Cheap and exception-free.
        self._agent_name = "agent"
        if context is not None and hasattr(context, "entrypoint"):
            self._agent_name = context.entrypoint or "agent"

        # Governance feature flag gate. When OFF: no extraction, no
        # ContextVar binding, no agent-type selector mutation, no
        # prefetch. All hook checks see _init_failed=True and no-op.
        # Everything below this point is governance-side state — running
        # it under FF-off would (a) violate the lazy/no-op contract and
        # (b) expose the wrapper to extraction exceptions on hosts where
        # governance is intentionally disabled.
        if not is_governance_enabled():
            self._init_failed = True
            self._evaluator_ready = True  # don't try to materialise later
            logger.info(
                "GovernanceRuntime initialized as no-op: governance feature "
                "flag is OFF (agent='%s', runtime_id='%s')",
                self._agent_name,
                runtime_id,
            )
            return

        try:
            # Bind the model-name ContextVar so adapters running in this
            # runtime's context see the right value under concurrent
            # agents. The token is stashed so dispose() can reset the
            # var — without that, the value leaks into sibling tasks
            # that inherit this context and outlive the runtime.
            model_name = self._extract_model_name(delegate, context)
            self._model_name = model_name
            self._model_name_token = _current_model_name.set(model_name)

            # Record agent-type before the policy prefetch fires so the
            # fetch can ask the server for the matching container key
            # (conversational vs autonomous).
            set_agent_conversational(
                self._extract_is_conversational(delegate, context)
            )

            # Fire the network-bound policy fetch in the background so
            # it overlaps with the rest of the agent setup. The
            # evaluator and adapter wrap are materialised lazily on the
            # first hook fire (see _ensure_evaluator), which is where
            # we wait for the prefetch to land.
            prefetch_policy_index()
            self._adapter_registry = get_adapter_registry()

            logger.info(
                "GovernanceRuntime initialized (prefetching policy): agent='%s', "
                "runtime_id='%s', model='%s', mode=%s, adapters=%d",
                self._agent_name,
                runtime_id,
                model_name or "unknown",
                get_enforcement_mode().value,
                len(self._adapter_registry.get_all()),
            )
        except Exception as e:
            # Fail-safe: log error but don't break runtime initialization
            self._init_failed = True
            self._evaluator = None
            self._adapter_registry = None
            logger.warning(
                "GovernanceRuntime initialization failed (continuing without governance): %s",
                e,
            )

    def _extract_model_name(
        self,
        delegate: UiPathRuntimeProtocol,
        context: Any,
    ) -> str:
        """Extract model name from delegate or context."""
        model_name = ""

        # Try _agent_definition.settings.model (LicensedRuntime pattern)
        agent_def = getattr(delegate, "_agent_definition", None)
        if agent_def:
            settings = getattr(agent_def, "settings", None)
            if settings:
                model_name = getattr(settings, "model", None) or ""

        # Try direct attributes on delegate
        if not model_name:
            for attr in ["model", "model_name", "_model", "model_id"]:
                val = getattr(delegate, attr, None)
                if val:
                    model_name = str(val)
                    break

        # Try nested delegate chain (unwrap wrappers)
        if not model_name:
            inner = getattr(delegate, "_delegate", None) or getattr(
                delegate, "delegate", None
            )
            while inner and not model_name:
                agent_def = getattr(inner, "_agent_definition", None)
                if agent_def:
                    settings = getattr(agent_def, "settings", None)
                    if settings:
                        model_name = getattr(settings, "model", None) or ""
                        break
                inner = getattr(inner, "_delegate", None) or getattr(
                    inner, "delegate", None
                )

        # Try context
        if not model_name and context is not None:
            for attr in ["model", "model_name", "model_id"]:
                val = getattr(context, attr, None)
                if val:
                    model_name = str(val)
                    break

        return model_name

    def _extract_is_conversational(
        self,
        delegate: UiPathRuntimeProtocol,
        context: Any,
    ) -> bool:
        """Determine whether the hosted agent is conversational.

        Reads ``AgentDefinition.is_conversational`` off the delegate's
        ``_agent_definition`` (the LicensedRuntime pattern, same source as
        :meth:`_extract_model_name`), unwrapping nested delegates. Falls back
        to the runtime context's conversation id when no agent definition is
        reachable. Defaults to ``False`` (autonomous) — fail-safe: an
        unknown agent is treated as autonomous, matching the server default.
        """

        def _from_agent_def(obj: Any) -> bool | None:
            agent_def = getattr(obj, "_agent_definition", None)
            if agent_def is None:
                return None
            value = getattr(agent_def, "is_conversational", None)
            return bool(value) if value is not None else None

        # Delegate, then the unwrapped delegate chain. Depth cap mirrors
        # :meth:`_extract_agent` — a pathological wrapper chain shouldn't
        # turn this synchronous init into a loop.
        node: Any = delegate
        for _ in range(10):
            if node is None:
                break
            result = _from_agent_def(node)
            if result is not None:
                return result
            node = getattr(node, "_delegate", None) or getattr(node, "delegate", None)

        # Fallback: a populated conversation id implies a conversational run.
        if context is not None:
            conversation_id = getattr(context, "conversation_id", None)
            if conversation_id:
                return True

        return False

    def _wrap_delegate_agent(self) -> None:
        """Extract agent from delegate and wrap with governance via adapters.

        This method:
        1. Searches delegate for known agent attributes
        2. Uses AdapterRegistry to find matching adapter
        3. Wraps agent with governance hooks
        4. Replaces original agent in delegate with governed version

        IMPORTANT: This method is fail-safe. Any error during adapter wrapping
        will continue without inner hooks. It will NEVER break the execution flow.
        BEFORE_AGENT/AFTER_AGENT checks at the runtime boundary still provide governance.
        """
        try:
            # Extract agent from delegate
            agent, attr_name = self._extract_agent(self._delegate)

            if agent is None:
                logger.debug(
                    "No agent found in delegate - continuing without inner hooks"
                )
                return

            if self._adapter_registry is None:
                # Defensive: should never happen because _ensure_evaluator
                # is gated on _init_failed which is set with adapter_registry=None.
                return

            # Find matching adapter
            adapter = self._adapter_registry.resolve(agent)
            if adapter is None:
                logger.debug(
                    "No adapter found for agent type '%s' - continuing without inner hooks",
                    type(agent).__name__,
                )
                return

            if self._evaluator is None:
                # _wrap_delegate_agent is now called from _ensure_evaluator
                # after the evaluator is materialised; this guard exists
                # only for defensive symmetry with wrap_agent().
                logger.debug("Skipping adapter attach: evaluator not yet materialised")
                return

            # Wrap agent with governance
            governed = adapter.attach(
                agent=agent,
                agent_id=self._agent_name,
                session_id=self._runtime_id,
                evaluator=self._evaluator,
            )

            install_delegation_guard(agent)

            # Store references
            self._original_agent = agent
            self._governed_agent = governed
            self._agent_attr_name = attr_name

            # Replace agent in delegate with governed version
            if attr_name is not None:
                self._replace_agent_in_delegate(governed, attr_name)

            logger.info(
                "Agent wrapped with governance: type=%s, adapter=%s, attr=%s",
                type(agent).__name__,
                adapter.name,
                attr_name,
            )

        except Exception as e:
            # Catch-all: ensure we never break execution
            logger.warning(
                "Agent wrapping failed: %s - continuing without inner hooks",
                e,
            )

    def _extract_agent(self, delegate: Any) -> tuple[Any, str | None]:
        """Extract agent from delegate runtime.

        Searches known attribute names in priority order.
        This method is fail-safe and will return (None, None) on any error.

        Returns:
            Tuple of (agent, attribute_name) or (None, None) if not found
        """
        try:
            # First check direct attributes on delegate
            for attr in self._AGENT_ATTRS:
                try:
                    agent = getattr(delegate, attr, None)
                    if agent is not None:
                        logger.debug("Found agent at delegate.%s", attr)
                        return agent, attr
                except Exception:
                    continue

            # Check nested delegate chain (unwrap wrapper layers)
            inner = getattr(delegate, "_delegate", None) or getattr(
                delegate, "delegate", None
            )
            depth = 0
            while inner is not None and depth < 10:  # Prevent infinite loops
                for attr in self._AGENT_ATTRS:
                    try:
                        agent = getattr(inner, attr, None)
                        if agent is not None:
                            logger.debug(
                                "Found agent at nested delegate.%s (depth=%d)",
                                attr,
                                depth,
                            )
                            # Return the inner delegate that holds the agent
                            self._agent_holder = inner
                            return agent, attr
                    except Exception:
                        continue
                inner = getattr(inner, "_delegate", None) or getattr(
                    inner, "delegate", None
                )
                depth += 1

        except Exception as e:
            logger.debug("Agent extraction failed: %s", e)

        return None, None

    def _replace_agent_in_delegate(self, governed: Any, attr_name: str) -> bool:
        """Replace original agent in delegate with governed version.

        This method is fail-safe and will not raise exceptions.

        Args:
            governed: The governed agent proxy
            attr_name: Attribute name where agent was found

        Returns:
            True if replacement succeeded, False otherwise
        """
        # If we found agent in a nested delegate, use that
        holder = getattr(self, "_agent_holder", None) or self._delegate

        try:
            setattr(holder, attr_name, governed)
            logger.debug("Replaced agent at %s.%s", type(holder).__name__, attr_name)
            return True
        except AttributeError:
            # Some objects have read-only attributes
            logger.debug(
                "Cannot replace agent at %s.%s (read-only) - adapter hooks still active",
                type(holder).__name__,
                attr_name,
            )
            return False
        except Exception as e:
            logger.debug(
                "Failed to replace agent at %s.%s: %s - adapter hooks still active",
                type(holder).__name__,
                attr_name,
                e,
            )
            return False

    def wrap_agent(self, agent: Any, agent_id: str | None = None) -> Any:
        """Wrap an agent with governance using the appropriate adapter.

        This method detects the agent framework and applies the correct adapter.

        Args:
            agent: The agent to wrap
            agent_id: Optional agent identifier (defaults to type name)

        Returns:
            Governed agent proxy
        """
        agent_id = agent_id or type(agent).__name__
        session_id = self._runtime_id

        if self._adapter_registry is None:
            logger.warning(
                "wrap_agent called but adapter registry not initialised "
                "(governance likely disabled by feature flag); returning agent unwrapped"
            )
            return agent

        # Find matching adapter
        adapter = self._adapter_registry.resolve(agent)
        if adapter is None:
            logger.warning("No adapter found for agent type: %s", type(agent).__name__)
            return agent

        if self._evaluator is None:
            logger.warning(
                "wrap_agent called before evaluator materialised; "
                "ensuring evaluator is ready before attaching adapter"
            )
            self._ensure_evaluator()
            if self._evaluator is None:
                logger.warning(
                    "Evaluator failed to materialise; returning agent unwrapped"
                )
                return agent

        # Attach governance via adapter
        governed = adapter.attach(
            agent=agent,
            agent_id=agent_id,
            session_id=session_id,
            evaluator=self._evaluator,
        )

        logger.info(
            "Agent wrapped with governance: agent=%s, adapter=%s",
            agent_id,
            adapter.name,
        )

        return governed

    def _ensure_evaluator(self) -> None:
        """Materialise the evaluator and attach adapters on first hook fire.

        Idempotent — subsequent calls are no-ops. The first call blocks
        on the policy prefetch that ``__init__`` kicked off; the user
        request was explicit that the wait happens here, not at init.
        """
        if self._evaluator_ready or self._init_failed:
            return
        with self._evaluator_lock:
            if self._evaluator_ready or self._init_failed:
                return
            try:
                policy_index = get_policy_index()
                self._evaluator = GovernanceEvaluator(policy_index)
                self._wrap_delegate_agent()
                logger.info(
                    "GovernanceRuntime ready: agent='%s', packs=%s, rules=%d, wrapped=%s",
                    self._agent_name,
                    policy_index.pack_names,
                    policy_index.total_rules,
                    self._governed_agent is not None,
                )
            except Exception as exc:
                self._init_failed = True
                self._evaluator = None
                logger.warning(
                    "Lazy evaluator materialisation failed; "
                    "governance disabled for this runtime: %s",
                    exc,
                )
            finally:
                self._evaluator_ready = True

    def _check_before_agent(self, input: Dict[str, Any] | None) -> None:
        """Evaluate BEFORE_AGENT rules at runtime boundary.

        The evaluator owns audit emission and DENY-raising; this method
        just primes the evaluator and propagates blocks.

        Fail-safe: Only GovernanceBlockException propagates. All other
        errors are logged and execution continues.
        """
        try:
            self._ensure_evaluator()
            if self._init_failed or self._evaluator is None:
                return

            agent_input = _extract_governable_text(input)

            self._evaluator.evaluate_before_agent(
                agent_input=agent_input,
                agent_name=self._agent_name,
                runtime_id=self._runtime_id,
                trace_id=self._trace_id,
                model_name=self._model_name,
            )

        except GovernanceBlockException:
            raise  # Allow intentional blocks to propagate
        except Exception as e:
            # Fail-safe: log and continue without blocking
            logger.warning("BEFORE_AGENT governance check failed (continuing): %s", e)

    def _check_after_agent(self, output: Any) -> None:
        """Evaluate AFTER_AGENT rules at runtime boundary.

        The evaluator owns audit emission and DENY-raising; this method
        just primes the evaluator and propagates blocks.

        Fail-safe: Only GovernanceBlockException propagates. All other
        errors are logged and execution continues.
        """
        try:
            self._ensure_evaluator()
            if self._init_failed or self._evaluator is None:
                return

            # Pull the agent's textual output for governance. UiPathRuntimeResult
            # wraps the actual payload under ``.output``; everything else (raw
            # dicts, strings, framework result objects) goes through the
            # extractor directly. The extractor handles list-of-blocks,
            # dict-content, pydantic, etc. — no more dict-repr garble.
            payload: Any = getattr(output, "output", output)
            agent_output = _extract_governable_text(payload)

            self._evaluator.evaluate_after_agent(
                agent_output=agent_output,
                agent_name=self._agent_name,
                runtime_id=self._runtime_id,
                trace_id=self._trace_id,
            )

        except GovernanceBlockException:
            raise  # Allow intentional blocks to propagate
        except Exception as e:
            # Fail-safe: log and continue without blocking
            logger.warning("AFTER_AGENT governance check failed (continuing): %s", e)

    @property
    def delegate(self) -> UiPathRuntimeProtocol:
        """Return the wrapped runtime this proxy delegates to."""
        return self._delegate

    @property
    def evaluator(self) -> GovernanceEvaluator | None:
        """Get the governance evaluator (None until first hook fires)."""
        return self._evaluator

    async def execute(
        self,
        input: Dict[str, Any] | None = None,
        options: Optional[Any] = None,
    ) -> Any:
        """Execute with governance checks at runtime boundary."""
        # BEFORE_AGENT: Check input before any agent execution
        self._check_before_agent(input)

        # Delegate to actual runtime
        result = await self._delegate.execute(input, options)

        # AFTER_AGENT: Check output before returning
        self._check_after_agent(result)

        return result

    async def stream(
        self,
        input: Dict[str, Any] | None = None,
        options: Optional[Any] = None,
    ) -> AsyncGenerator[Any, None]:
        """Stream with governance checks at runtime boundary."""
        # BEFORE_AGENT: Check input before any agent execution
        self._check_before_agent(input)

        # Delegate to actual runtime and collect final result. The
        # terminal stream event is detected structurally via the
        # Same-package class import — no cross-package shim needed.
        # importing the concrete UiPathRuntimeResult class from
        # uipath-runtime (which would create a core→runtime cycle).
        final_result = None
        async for event in self._delegate.stream(input, options):
            if isinstance(event, UiPathRuntimeResult):
                final_result = event
            yield event

        # AFTER_AGENT: Check output after stream completes
        if final_result is not None:
            self._check_after_agent(final_result)

    async def get_schema(self) -> Any:
        """Delegate to the wrapped runtime; governance has no schema layer."""
        return await self._delegate.get_schema()

    async def dispose(self) -> None:
        """Dispose the runtime and restore original agent if wrapped.

        Each governance-side cleanup step is isolated so a single
        failure can't strand later steps. ``self._delegate.dispose()``
        always runs last and is the only step whose exception is allowed
        to propagate — that's the caller's contract with the wrapped
        runtime.
        """
        # Step 1: restore the original agent attribute.
        if self._original_agent is not None and self._agent_attr_name is not None:
            try:
                holder = self._agent_holder or self._delegate
                setattr(holder, self._agent_attr_name, self._original_agent)
                logger.debug("Restored original agent on dispose")
            except Exception as e:
                logger.warning("Failed to restore original agent: %s", e)

        # Step 2: uninstall the delegation guard (its own try/except so
        # a failed restore in step 1 doesn't skip the guard removal).
        if self._original_agent is not None:
            try:
                uninstall_delegation_guard(self._original_agent)
            except Exception as e:
                logger.warning("Failed to uninstall delegation guard: %s", e)

        # Step 3: reset the model-name ContextVar so the value doesn't
        # leak into sibling tasks that inherited this context.
        if self._model_name_token is not None:
            try:
                _current_model_name.reset(self._model_name_token)
            except ValueError:
                # Token created in a different context — happens when
                # dispose runs from a child task. Accept the leak rather
                # than calling set(""), which would create yet another
                # ContextVar level on top of the original.
                logger.debug("Model-name ContextVar reset from foreign context")
            except Exception as e:
                logger.warning("Failed to reset model-name context: %s", e)
            finally:
                self._model_name_token = None

        # Step 4: delegate dispose — always last, exception propagates.
        # The caller controls the contract with the wrapped runtime; we
        # don't swallow its cleanup failures.
        await self._delegate.dispose()

    def __getattr__(self, name: str) -> Any:
        """Forward non-protocol attribute access to the wrapped runtime."""
        # Guard against recursion when __init__ raises before _delegate is
        # bound, or against probes for our own private attributes that
        # haven't been set yet.
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            delegate = object.__getattribute__(self, "_delegate")
        except AttributeError as exc:
            raise AttributeError(name) from exc
        return getattr(delegate, name)


def governance_wrapper(
    runtime: UiPathRuntimeProtocol,
    context: Any,
    runtime_id: str,
) -> UiPathRuntimeProtocol:
    """Wrapper function for UiPathRuntimeWrapperRegistry.

    Creates a GovernanceRuntime that wraps the given runtime with
    compliance evaluation at each lifecycle hook.

    Args:
        runtime: The runtime to wrap
        context: Runtime context from the SDK (only ``entrypoint`` is read)
        runtime_id: Unique identifier for this runtime instance
    """
    if not is_governance_enabled():
        logger.debug(
            "governance_wrapper: %s feature flag is OFF; returning unwrapped runtime",
            "EnablePythonGovernanceChecker",
        )
        return runtime
    mode = get_enforcement_mode()
    if mode == EnforcementMode.DISABLED:
        logger.debug("Governance disabled - returning unwrapped runtime")
        return runtime
    return GovernanceRuntime(runtime, context, runtime_id)


def wrap_agent(agent: Any, agent_id: str | None = None) -> Any:
    """Convenience function to wrap an agent with governance.

    Uses the AdapterRegistry to detect the framework and apply appropriate hooks.

    Args:
        agent: The agent to wrap
        agent_id: Optional agent identifier

    Returns:
        Governed agent proxy

    Example:
        from uipath.core.governance import wrap_agent

        governed_agent = wrap_agent(my_langchain_agent, "my-agent")
        result = governed_agent.invoke({"input": "Hello"})
    """
    if not is_governance_enabled():
        logger.debug(
            "wrap_agent: %s feature flag is OFF; returning unwrapped agent",
            "EnablePythonGovernanceChecker",
        )
        return agent

    mode = get_enforcement_mode()
    if mode == EnforcementMode.DISABLED:
        logger.debug("Governance disabled - returning unwrapped agent")
        return agent

    agent_id = agent_id or type(agent).__name__
    session_id = str(uuid4())

    # Get evaluator
    policy_index = get_policy_index()
    evaluator = GovernanceEvaluator(policy_index)

    # Find matching adapter
    registry = get_adapter_registry()
    adapter = registry.resolve(agent)

    if adapter is None:
        logger.warning("No adapter found for agent type: %s", type(agent).__name__)
        return agent

    # Attach governance
    governed = adapter.attach(
        agent=agent,
        agent_id=agent_id,
        session_id=session_id,
        evaluator=evaluator,
    )

    logger.info(
        "Agent wrapped: agent=%s, adapter=%s, packs=%s",
        agent_id,
        adapter.name,
        policy_index.pack_names,
    )

    return governed
