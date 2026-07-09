"""WASM-based Rego governance evaluator using opa-wasmtime."""
from __future__ import annotations

import io
import logging
import os
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from uipath.core.governance import EnforcementMode
from uipath.core.governance.exceptions import GovernanceBlockException
from uipath.core.governance.models import (
    Action,
    AuditRecord,
    LifecycleHook,
    RuleEvaluation,
)

from uipath.runtime.governance._audit.base import AuditManager
from uipath.runtime.governance.config import get_enforcement_mode
from uipath.runtime.governance.features import compute_features
from uipath.runtime.governance.native.models import CheckContext

logger = logging.getLogger(__name__)

_WARMUP_INPUT: dict[str, Any] = {
    "hook": "before_model",
    "agent_input": "", "agent_output": "",
    "model_input": "", "model_output": "",
    "model_name": "", "agent_name": "",
    "tool_name": "", "tool_args": {}, "tool_result": "",
    "session_state": {"tool_calls": 0, "llm_calls": 0},
}


def context_to_input(
    context: CheckContext,
    feature_plan: list[str] | None = None,
) -> dict[str, Any]:
    """Serialize a CheckContext to the flat input dict Rego rules expect."""
    session = context.session_state if isinstance(context.session_state, dict) else {}
    features = compute_features(context, feature_plan) if feature_plan else {}
    return {
        "hook": context.hook.value,
        "agent_input": context.agent_input,
        "agent_output": context.agent_output,
        "model_input": context.model_input,
        "model_output": context.model_output,
        "model_name": context.model_name,
        "agent_name": context.agent_name,
        "tool_name": context.tool_name,
        "tool_args": context.tool_args,
        "tool_result": context.tool_result,
        "session_state": {
            "tool_calls": session.get("tool_calls", 0),
            "llm_calls": session.get("llm_calls", 0),
        },
        "ring": context.ring,
        "messages": context.messages,
        "features": features,
    }


def _extract_wasm_from_bundle(bundle_path: Path) -> bytes:
    """Extract ``policy.wasm`` bytes from an OPA ``.tar.gz`` bundle."""
    with open(bundle_path, "rb") as f:
        bundle_bytes = f.read()
    with tarfile.open(fileobj=io.BytesIO(bundle_bytes), mode="r:gz") as tf:
        try:
            member = tf.getmember("/policy.wasm")
        except KeyError:
            member = tf.getmember("policy.wasm")
        fobj = tf.extractfile(member)
        if fobj is None:
            raise ValueError(f"policy.wasm is not a regular file in {bundle_path}")
        return fobj.read()


def _extract_data_json_from_bundle(bundle_path: Path) -> dict[str, Any] | None:
    """Extract and parse ``data.json`` from an OPA ``.tar.gz`` bundle.

    Returns None when the bundle predates the data.json format.
    """
    import json as _json

    with open(bundle_path, "rb") as f:
        bundle_bytes = f.read()
    with tarfile.open(fileobj=io.BytesIO(bundle_bytes), mode="r:gz") as tf:
        for candidate in ("/data.json", "data.json"):
            try:
                member = tf.getmember(candidate)
            except KeyError:
                continue
            fobj = tf.extractfile(member)
            if fobj is None:
                continue
            return _json.loads(fobj.read().decode("utf-8"))
    return None


def _load_engine(bundle_path: Path) -> Any:
    """Load a WASM bundle via opa-wasmtime. Raises on any failure."""
    from opa_wasmtime import OPAPolicy  # type: ignore[import]

    wasm_bytes = _extract_wasm_from_bundle(bundle_path)
    with tempfile.NamedTemporaryFile(suffix=".wasm", delete=False) as tmp:
        tmp.write(wasm_bytes)
        tmp_path = tmp.name
    try:
        return OPAPolicy(tmp_path)
    finally:
        os.unlink(tmp_path)


def _pack_name_from_rule_id(rule_id: str) -> str:
    """Extract the pack/policy name prefix from ``{policyId}/{ruleId}``."""
    return rule_id.split("/")[0] if "/" in rule_id else "custom"


class RegoEvaluator:
    """WASM-based Rego evaluator with one engine per lifecycle hook.

    Args:
        hook_wasm_paths: Map of LifecycleHook → bundle path on disk.
        hook_data: Optional map of LifecycleHook → data.json dict.
        audit_manager: Optional instance-scoped :class:`AuditManager`.
            When provided, rule evaluation and hook summary events are
            emitted to it. When ``None``, audit emission is skipped.
        enforcement_mode: Optional enforcement mode override. When
            ``None``, :func:`get_enforcement_mode` is called on each
            evaluate() to read the process-level value set by the native
            policy loader.
    """

    def __init__(
        self,
        hook_wasm_paths: dict[LifecycleHook, Path],
        hook_data: dict[LifecycleHook, dict[str, Any]] | None = None,
        audit_manager: AuditManager | None = None,
        enforcement_mode: EnforcementMode | None = None,
    ) -> None:
        """Load WASM engines from paths; wire audit manager and enforcement mode."""
        self._engines: dict[LifecycleHook, Any] = {}
        self._feature_plans: dict[LifecycleHook, list[str]] = {}
        self._audit_manager = audit_manager
        self._enforcement_mode = enforcement_mode
        hook_data = hook_data or {}

        for hook, path in hook_wasm_paths.items():
            try:
                engine = _load_engine(path)
                data = hook_data.get(hook)
                if data:
                    try:
                        engine.set_data(data)
                    except Exception as exc:
                        logger.warning(
                            "Rego WASM set_data failed for hook=%s: %s (continuing without data)",
                            hook.value, exc,
                        )
                    self._feature_plans[hook] = list(data.get("required_features") or [])
                try:
                    engine.evaluate(_WARMUP_INPUT)
                except Exception as warmup_exc:
                    logger.warning(
                        "Rego WASM warmup failed for hook=%s: %s (engine kept)",
                        hook.value, warmup_exc,
                    )
                self._engines[hook] = engine
                logger.info("Loaded Rego WASM for hook=%s from %s", hook.value, path)
            except ImportError:
                logger.warning(
                    "opa-wasmtime not installed; Rego evaluation for hook=%s disabled. "
                    "Install with: pip install uipath-runtime[governance-rego]",
                    hook.value,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to load Rego WASM for hook=%s from %s: %s",
                    hook.value, path, exc,
                )

    @property
    def loaded_hooks(self) -> list[LifecycleHook]:
        """Lifecycle hooks for which WASM engines were loaded successfully."""
        return list(self._engines)

    def _resolve_mode(self) -> EnforcementMode:
        """Return the enforcement mode for this evaluation."""
        return self._enforcement_mode if self._enforcement_mode is not None else get_enforcement_mode()

    def evaluate(self, context: CheckContext) -> AuditRecord:
        """Evaluate a lifecycle hook against loaded Rego policies."""
        mode = self._resolve_mode()

        if mode == EnforcementMode.DISABLED:
            return AuditRecord(
                timestamp=datetime.now(timezone.utc),
                agent_name=context.agent_name,
                runtime_id=context.runtime_id,
                trace_id="",
                hook=context.hook,
                evaluations=[],
                final_action=Action.ALLOW,
                metadata={"enforcement_mode": mode.value, "rego_engine": "skipped"},
            )

        engine = self._engines.get(context.hook)
        if engine is None:
            return AuditRecord(
                timestamp=datetime.now(timezone.utc),
                agent_name=context.agent_name,
                runtime_id=context.runtime_id,
                trace_id="",
                hook=context.hook,
                evaluations=[],
                final_action=Action.ALLOW,
                metadata={"enforcement_mode": mode.value, "rego_engine": "missing"},
            )

        try:
            plan = self._feature_plans.get(context.hook, [])
            raw = engine.evaluate(context_to_input(context, feature_plan=plan))
        except Exception as exc:
            logger.warning("Rego WASM evaluation failed for hook=%s: %s", context.hook.value, exc)
            return AuditRecord(
                timestamp=datetime.now(timezone.utc),
                agent_name=context.agent_name,
                runtime_id=context.runtime_id,
                trace_id="",
                hook=context.hook,
                evaluations=[],
                final_action=Action.ALLOW,
                metadata={"enforcement_mode": mode.value, "rego_engine": "error"},
            )

        result: dict[str, Any] = {}
        if isinstance(raw, list) and raw:
            result = raw[0].get("result", {})
        elif isinstance(raw, dict):
            result = raw

        fired_deny: set[str] = set(result.get("fired_deny") or [])
        fired_allow: set[str] = set(result.get("fired_allow") or [])
        rule_messages: dict[str, str] = dict(result.get("messages") or {})

        evaluations: list[RuleEvaluation] = []
        for rule_id in fired_deny:
            evaluations.append(RuleEvaluation(
                rule_id=rule_id,
                rule_name=rule_id,
                matched=True,
                pack_name=_pack_name_from_rule_id(rule_id),
                action=Action.DENY,
                detail=rule_messages.get(rule_id, f"Rego rule '{rule_id}' denied the request"),
                description="",
            ))
        for rule_id in fired_allow:
            evaluations.append(RuleEvaluation(
                rule_id=rule_id,
                rule_name=rule_id,
                matched=True,
                pack_name=_pack_name_from_rule_id(rule_id),
                action=Action.ALLOW,
                detail=rule_messages.get(rule_id, f"Rego rule '{rule_id}' explicitly allowed"),
                description="",
            ))

        raw_action = Action.DENY if bool(result.get("deny", False)) else Action.ALLOW
        final_action = self._apply_mode(raw_action, mode)

        metadata: dict[str, Any] = {"enforcement_mode": mode.value}
        if raw_action == Action.DENY and mode == EnforcementMode.AUDIT:
            metadata["audit_mode_would_deny"] = True

        audit = AuditRecord(
            timestamp=datetime.now(timezone.utc),
            agent_name=context.agent_name,
            runtime_id=context.runtime_id,
            trace_id="",
            hook=context.hook,
            evaluations=evaluations,
            final_action=final_action,
            metadata=metadata,
        )

        self._emit_audit(audit, mode)

        if final_action == Action.DENY:
            raise GovernanceBlockException.from_audit_record(audit)

        return audit

    def _apply_mode(self, raw_action: Action, mode: EnforcementMode) -> Action:
        """Downgrade DENY to AUDIT when mode is not ENFORCE."""
        if mode == EnforcementMode.AUDIT and raw_action in (Action.DENY, Action.ESCALATE):
            return Action.AUDIT
        return raw_action

    def _emit_audit(self, audit: AuditRecord, mode: EnforcementMode) -> None:
        """Emit per-rule and hook-summary events to the injected AuditManager."""
        if self._audit_manager is None:
            return
        hook_name = audit.hook.name
        for ev in audit.evaluations:
            try:
                self._audit_manager.emit_rule_evaluation(
                    policy_id=ev.rule_id,
                    rule_name=ev.rule_name,
                    pack_name=ev.pack_name,
                    hook=hook_name,
                    matched=ev.matched,
                    action=ev.action.value if ev.matched else "allow",
                    enforcement_mode=mode,
                    detail=ev.detail,
                    agent_name=audit.agent_name,
                    description=ev.description,
                )
            except Exception:  # noqa: BLE001
                pass
        try:
            self._audit_manager.emit_hook_summary(
                hook=hook_name,
                agent_name=audit.agent_name,
                total_rules=len(audit.evaluations),
                matched_rules=sum(1 for e in audit.evaluations if e.matched),
                final_action=audit.final_action.value,
                enforcement_mode=mode,
            )
        except Exception:  # noqa: BLE001
            pass

    def evaluate_before_agent(
        self,
        agent_input: str,
        agent_name: str,
        runtime_id: str,
        model_name: str = "",
        **kwargs: Any,
    ) -> AuditRecord:
        """Evaluate the BEFORE_AGENT lifecycle hook."""
        ctx = CheckContext(
            hook=LifecycleHook.BEFORE_AGENT,
            agent_name=agent_name,
            runtime_id=runtime_id,
            agent_input=agent_input,
            model_name=model_name,
            metadata=kwargs.get("metadata", {}),
        )
        return self.evaluate(ctx)

    def evaluate_after_agent(
        self,
        agent_output: str,
        agent_name: str,
        runtime_id: str,
        **kwargs: Any,
    ) -> AuditRecord:
        """Evaluate the AFTER_AGENT lifecycle hook."""
        ctx = CheckContext(
            hook=LifecycleHook.AFTER_AGENT,
            agent_name=agent_name,
            runtime_id=runtime_id,
            agent_output=agent_output,
            metadata=kwargs.get("metadata", {}),
        )
        return self.evaluate(ctx)

    def evaluate_before_model(
        self,
        model_input: str,
        agent_name: str,
        runtime_id: str,
        messages: list[dict[str, Any]] | None = None,
        model_name: str = "",
        **kwargs: Any,
    ) -> AuditRecord:
        """Evaluate the BEFORE_MODEL lifecycle hook."""
        ctx = CheckContext(
            hook=LifecycleHook.BEFORE_MODEL,
            agent_name=agent_name,
            runtime_id=runtime_id,
            model_input=model_input,
            model_name=model_name,
            messages=messages or [],
            metadata=kwargs.get("metadata", {}),
        )
        return self.evaluate(ctx)

    def evaluate_after_model(
        self,
        model_output: str,
        agent_name: str,
        runtime_id: str,
        **kwargs: Any,
    ) -> AuditRecord:
        """Evaluate the AFTER_MODEL lifecycle hook."""
        ctx = CheckContext(
            hook=LifecycleHook.AFTER_MODEL,
            agent_name=agent_name,
            runtime_id=runtime_id,
            model_output=model_output,
            metadata=kwargs.get("metadata", {}),
        )
        return self.evaluate(ctx)

    def evaluate_tool_call(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        agent_name: str,
        runtime_id: str,
        session_state: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> AuditRecord:
        """Evaluate the TOOL_CALL lifecycle hook."""
        ctx = CheckContext(
            hook=LifecycleHook.TOOL_CALL,
            agent_name=agent_name,
            runtime_id=runtime_id,
            tool_name=tool_name,
            tool_args=tool_args,
            session_state=session_state or {},
            metadata=kwargs.get("metadata", {}),
        )
        return self.evaluate(ctx)

    def evaluate_after_tool(
        self,
        tool_name: str,
        tool_result: str,
        agent_name: str,
        runtime_id: str,
        **kwargs: Any,
    ) -> AuditRecord:
        """Evaluate the AFTER_TOOL lifecycle hook."""
        ctx = CheckContext(
            hook=LifecycleHook.AFTER_TOOL,
            agent_name=agent_name,
            runtime_id=runtime_id,
            tool_name=tool_name,
            tool_result=tool_result,
            metadata=kwargs.get("metadata", {}),
        )
        return self.evaluate(ctx)
