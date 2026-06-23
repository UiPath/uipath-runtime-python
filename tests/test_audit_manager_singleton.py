"""Tests for ``get_audit_manager`` singleton + thread-safe lazy init.

The global manager is constructed on first call and reused thereafter.
A previous version did the lazy init without a lock — two threads
hitting the first call simultaneously could each construct their own
manager, leaking a worker thread and splitting audit traffic. These
tests pin the double-checked-locked init: every concurrent first
caller must receive the exact same instance.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from uipath.runtime.governance._audit.base import (
    AuditManager,
    get_audit_manager,
    reset_audit_manager,
)


@pytest.fixture(autouse=True)
def _reset_global() -> None:
    """Ensure each test starts and ends without a global manager."""
    reset_audit_manager()
    yield
    reset_audit_manager()


def test_returns_same_instance_on_repeat_calls() -> None:
    """Sequential calls share one manager."""
    first = get_audit_manager()
    second = get_audit_manager()
    assert first is second
    assert isinstance(first, AuditManager)


def test_concurrent_first_calls_get_same_instance() -> None:
    """No two concurrent callers may observe different managers.

    Spin up many threads that all block on a barrier, then race into
    ``get_audit_manager``. Without the lock, two threads could each
    win the ``is None`` check and construct their own manager. With
    the lock, exactly one instance is created and every thread
    returns it.
    """
    thread_count = 32
    barrier = threading.Barrier(thread_count)
    instances: list[AuditManager] = []
    instances_lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        m = get_audit_manager()
        with instances_lock:
            instances.append(m)

    with ThreadPoolExecutor(max_workers=thread_count) as pool:
        futures = [pool.submit(worker) for _ in range(thread_count)]
        for f in futures:
            f.result()

    assert len(instances) == thread_count
    first = instances[0]
    # Every thread must return the identical instance.
    assert all(m is first for m in instances), (
        "concurrent first calls produced multiple AuditManager instances"
    )


def test_reset_then_get_constructs_fresh_instance() -> None:
    """After reset, the next get returns a new manager (not the closed one)."""
    first = get_audit_manager()
    reset_audit_manager()
    second = get_audit_manager()
    assert first is not second
    assert isinstance(second, AuditManager)
