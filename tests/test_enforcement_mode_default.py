"""Tests for the default enforcement-mode resolution on :class:`PolicyLoader`.

The default is :attr:`EnforcementMode.AUDIT` so the wrapper attaches at
runtime construction and the background policy load can run. If the
provider later returns ``disabled``, the loader records it and
:attr:`enforcement_mode` flips.

Resolution (per :attr:`PolicyLoader.enforcement_mode`):
1. The provider-supplied value on the most recent load.
2. Default :attr:`EnforcementMode.AUDIT`.
"""

from __future__ import annotations

from uipath.core.governance import EnforcementMode, PolicyResponse

from tests._helpers import StubPolicyProvider
from uipath.runtime.governance.native.loader import PolicyLoader


def test_default_mode_is_audit() -> None:
    """No provider-supplied mode yet → AUDIT.

    AUDIT is the default so the wrapper attaches and the background
    policy fetch can run. The backend can flip the mode to DISABLED
    on fetch when the tenant has no policies.
    """
    loader = PolicyLoader(None)
    assert loader.enforcement_mode is EnforcementMode.AUDIT


def test_provider_disabled_wins_over_default() -> None:
    """A provider supplying DISABLED overrides the AUDIT default."""
    provider = StubPolicyProvider(
        response=PolicyResponse(mode=EnforcementMode.DISABLED, policies="")
    )
    loader = PolicyLoader(provider)
    loader.load_policy_index()
    assert loader.enforcement_mode is EnforcementMode.DISABLED


def test_provider_enforce_wins_over_default() -> None:
    """A provider supplying ENFORCE flips the loader to enforce."""
    provider = StubPolicyProvider(
        response=PolicyResponse(
            mode=EnforcementMode.ENFORCE,
            policies="standard: p\nrules: [{id: r1, hook: before_model, "
            "checks: [{type: regex, patterns: ['x']}]}]\n",
        )
    )
    loader = PolicyLoader(provider)
    loader.load_policy_index()
    assert loader.enforcement_mode is EnforcementMode.ENFORCE


def test_loader_with_none_mode_response_keeps_previous_value() -> None:
    """Provider returning ``mode=None`` doesn't clobber a previously-set mode.

    The wire response model treats ``None`` as "no opinion" — the loader
    must not overwrite a real value with it. Otherwise a transient
    provider response could silently demote a tenant's enforcement
    posture.
    """
    p1 = StubPolicyProvider(
        response=PolicyResponse(
            mode=EnforcementMode.ENFORCE,
            policies="standard: p\nrules: [{id: r1, hook: before_model, "
            "checks: [{type: regex, patterns: ['x']}]}]\n",
        )
    )
    loader = PolicyLoader(p1)
    loader.load_policy_index()
    assert loader.enforcement_mode is EnforcementMode.ENFORCE

    # A second provider response that omits mode should not flip back to AUDIT.
    loader._provider = StubPolicyProvider(
        response=PolicyResponse(
            mode=None,
            policies="standard: p\nrules: [{id: r1, hook: before_model, "
            "checks: [{type: regex, patterns: ['x']}]}]\n",
        )
    )
    loader.clear_cache()
    loader.load_policy_index()
    assert loader.enforcement_mode is EnforcementMode.ENFORCE


def test_two_loaders_carry_independent_enforcement_modes() -> None:
    """The whole point of the refactor: parallel loaders don't share mode.

    Previously :func:`set_enforcement_mode` wrote a module global, so an
    ENFORCE-mode loader and a DISABLED-mode loader running concurrently
    in the same process clobbered each other (last writer wins).
    Instance-scoped mode means each loader's mode is read-isolated.
    """
    p_enforce = StubPolicyProvider(
        response=PolicyResponse(
            mode=EnforcementMode.ENFORCE,
            policies="standard: e\nrules: [{id: r1, hook: before_model, "
            "checks: [{type: regex, patterns: ['x']}]}]\n",
        )
    )
    p_disabled = StubPolicyProvider(
        response=PolicyResponse(mode=EnforcementMode.DISABLED, policies="")
    )

    enforce_loader = PolicyLoader(p_enforce)
    disabled_loader = PolicyLoader(p_disabled)

    enforce_loader.load_policy_index()
    disabled_loader.load_policy_index()

    assert enforce_loader.enforcement_mode is EnforcementMode.ENFORCE
    assert disabled_loader.enforcement_mode is EnforcementMode.DISABLED
