"""Lifecycle tests for :class:`AuditManager`.

Pins the production-readiness invariants of the audit manager:

- Process cleanup uses a single ``atexit`` handler that walks a
  ``WeakSet`` — so creating many managers in one process doesn't
  bloat the atexit list and doesn't pin managers in memory.
- The fork-rebuild path is lock-protected: two threads in a
  freshly-forked child can't both rebuild the queue/worker
  concurrently.
"""

from __future__ import annotations

import gc
import os
import threading
from typing import Any
from unittest.mock import patch

import pytest

from uipath.runtime.governance._audit import base as audit_base
from uipath.runtime.governance._audit.base import AuditManager


def _bare_manager() -> AuditManager:
    """Build a manager with no default sinks (no traces sink, no atexit-set add)."""
    return AuditManager(async_mode=False, register_default_sinks=False)


# ---------------------------------------------------------------------------
# atexit accounting: one process-level hook, no per-instance accumulation
# ---------------------------------------------------------------------------


def test_default_managers_register_once_in_process_atexit() -> None:
    """Creating N managers must NOT add N entries to interpreter atexit.

    Regression: per-instance ``atexit.register(self._atexit_cleanup)``
    grew the atexit list linearly and held a strong ref to each manager.
    The fix routes everyone through one process-level cleanup hook.
    """
    with patch.object(audit_base.atexit, "register") as mock_register:
        # Reset module state so the assertion is deterministic
        # regardless of test-order side effects.
        audit_base._atexit_registered = False
        try:
            AuditManager(async_mode=False)  # first → registers
            AuditManager(async_mode=False)  # second → reuses
            AuditManager(async_mode=False)  # third  → reuses
            assert mock_register.call_count == 1, (
                "Each AuditManager must NOT register its own atexit handler"
            )
        finally:
            # Drop test managers from the cleanup set before leaving.
            audit_base._live_managers.clear()


def test_register_default_sinks_false_skips_cleanup_set() -> None:
    """Bare managers (tests) are not tracked for process cleanup."""
    m = _bare_manager()
    assert m not in audit_base._live_managers


def test_disposed_manager_can_be_garbage_collected() -> None:
    """The WeakSet must NOT keep a disposed manager alive.

    Regression: per-instance atexit held a strong ref → disposed
    managers leaked until process exit. With ``WeakSet`` + a single
    process hook, dropping the last reference lets the manager GC.
    """
    import weakref

    manager = AuditManager(async_mode=False)
    ref = weakref.ref(manager)

    # Sanity: it's tracked while alive.
    assert manager in audit_base._live_managers

    # Drop the local strong ref + force collection.
    del manager
    gc.collect()

    # The WeakSet entry must be gone (or about to be).
    assert ref() is None, (
        "AuditManager was kept alive — strong reference leak in cleanup machinery"
    )


def test_process_cleanup_handles_already_closed_manager() -> None:
    """If a manager was explicitly closed, the process hook is a no-op for it.

    A manager that called close() during normal lifecycle should not
    raise from the process-level cleanup — sink list is empty, worker
    is already joined.
    """
    m = AuditManager(async_mode=False)
    m.close()
    # Must not raise.
    audit_base._process_cleanup_managers()


# ---------------------------------------------------------------------------
# Fork-rebuild safety
# ---------------------------------------------------------------------------


def test_ensure_alive_after_fork_is_idempotent_under_concurrent_emit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two threads in a fresh-fork child must not both rebuild the queue.

    Without the lock, both threads observed the stale ``_pid``, both
    constructed a new ``queue.Queue`` / ``threading.Event`` /
    ``threading.Thread``, and the later writer leaked the earlier
    one's queue+worker. With the lock the loser sees the updated
    ``_pid`` after acquiring and returns.
    """
    m = AuditManager(async_mode=True, register_default_sinks=False)

    # Capture the post-construction queue + worker so we can detect
    # whether multiple rebuild winners occurred.
    original_queue = m._queue
    original_worker = m._worker_thread

    # Simulate a fork by mutating the recorded pid. We do NOT actually
    # fork; we just put the manager into "I think I'm in a stale
    # process" state.
    m._pid = -1

    barrier = threading.Barrier(8)
    seen_queues: set[int] = set()
    seen_workers: set[int] = set()
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        m._ensure_alive_after_fork()
        with lock:
            seen_queues.add(id(m._queue))
            seen_workers.add(id(m._worker_thread))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    # Exactly one queue + worker survived the race.
    assert len(seen_queues) == 1, (
        f"Multiple queues survived fork-rebuild race: {seen_queues}"
    )
    assert len(seen_workers) == 1, (
        f"Multiple workers survived fork-rebuild race: {seen_workers}"
    )
    # And the survivor is NOT the original (we did rebuild).
    assert original_queue is not m._queue
    assert original_worker is not m._worker_thread
    assert m._pid == os.getpid()

    m.close()


def test_ensure_alive_after_fork_fast_path_when_pid_unchanged() -> None:
    """Same-process call must NOT rebuild — sanity check on the fast path."""
    m = AuditManager(async_mode=True, register_default_sinks=False)
    original_queue = m._queue
    original_worker = m._worker_thread

    m._ensure_alive_after_fork()  # same PID — no-op

    assert m._queue is original_queue
    assert m._worker_thread is original_worker
    m.close()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_module_state() -> Any:
    """Test isolation for the module-level cleanup machinery.

    Sweep the WeakSet between tests so leftovers from one test don't
    show up in another. Don't reset ``_atexit_registered`` — once
    Python's ``atexit`` accepts a handler, we shouldn't unregister it
    just for tests, and the tests above that check registration count
    do their own reset under a patched ``atexit.register``.
    """
    yield
    audit_base._live_managers.clear()
