"""Shared test-only helpers.

Keeps test concerns out of the production governance package: per-test
isolation utilities and shared stubs live here rather than inside the
production modules.
"""

from __future__ import annotations

import time

from uipath.core.governance import PolicyContext, PolicyResponse

from uipath.runtime.governance import config


def reset_enforcement_mode() -> None:
    """Clear the process-wide enforcement mode so the AUDIT default re-applies.

    Test isolation only — production code never resets the mode; the policy
    loader sets it from the provider-supplied :class:`PolicyResponse`.
    """
    config._state.mode = None


class StubPolicyProvider:
    """Minimal in-memory :class:`GovernancePolicyProvider` for tests.

    Records every :class:`PolicyContext` it receives so tests can assert
    on the selector that travelled to the provider. Either returns a
    pre-canned :class:`PolicyResponse` or raises a pre-canned exception;
    the optional ``slow`` knob lets tests exercise the prefetch-wait
    path.
    """

    def __init__(
        self,
        response: PolicyResponse | None = None,
        raises: Exception | None = None,
        slow: float = 0.0,
    ):
        self.calls: list[PolicyContext] = []
        self._response = response
        self._raises = raises
        self._slow = slow

    def get_policy(self, context: PolicyContext) -> PolicyResponse:
        self.calls.append(context)
        if self._slow:
            time.sleep(self._slow)
        if self._raises is not None:
            raise self._raises
        assert self._response is not None
        return self._response
