"""uipath-ipc client that streams job logs to the handler (logs only)."""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
import threading
import time
from typing import Any

from uipath.runtime.jobapi.contract import IIpcLogSink, JobLogDto

_SEND_TIMEOUT_S = 1.0
_MAX_SEND_ATTEMPTS = 3
_RETRY_DELAY_S = 0.2
_MAX_CONSECUTIVE_DROPS = 3
_FAILURE_COOLDOWN_S = 30.0
_STARTUP_TIMEOUT_S = 10.0
_SHUTDOWN_TIMEOUT_S = 15.0

_STOP = object()


class IpcJobApiClient:
    """Owns a uipath-ipc client on a private event-loop thread.

    The runtime's ``__exit__`` is synchronous but IPC is asyncio-based, so the
    client runs its loop on its own thread (a Proactor loop on Windows, which
    named pipes require). Logs are enqueued without blocking the caller and
    drained one at a time, FIFO; the queue is flushed at job end. A send that
    fails is retried a few times, then that one entry is dropped, and after
    repeated failures forwarding is muted for a cooldown — mirroring the JS
    coded-functions log forwarder.
    """

    def __init__(self, endpoint: str, job_id: str, log: logging.Logger) -> None:
        """Configure the client; call ``start()`` to connect."""
        self._endpoint = endpoint
        self._job_id = job_id
        self._log = log
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._queue: asyncio.Queue[Any] | None = None
        self._proxy: IIpcLogSink | None = None
        self._client: Any = None
        self._consumer: asyncio.Task[None] | None = None
        self._ready = threading.Event()
        self._start_error: BaseException | None = None
        self._stopping = False

    def start(self) -> None:
        """Start the loop thread and connect; raise if that fails or times out."""
        if importlib.util.find_spec("uipath_ipc") is None:
            raise RuntimeError(
                "Job-API IPC was requested (UIPATH_JOB_API_IPC_ENDPOINT is set) but "
                "the 'uipath-ipc' package is not installed. Install it (e.g. "
                "'pip install uipath-ipc') to stream logs over IPC."
            )

        self._thread = threading.Thread(
            target=self._run, name="uipath-jobapi-ipc", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout=_STARTUP_TIMEOUT_S):
            raise RuntimeError(
                f"Job-API IPC client did not connect within {_STARTUP_TIMEOUT_S}s "
                f"(endpoint {self._endpoint!r})."
            )
        if self._start_error is not None:
            raise self._start_error

    def _run(self) -> None:
        try:
            if sys.platform == "win32":
                self._loop = asyncio.ProactorEventLoop()
            else:
                self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._setup())
        except BaseException as e:
            self._start_error = e
            self._ready.set()
            return
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    async def _setup(self) -> None:
        from uipath_ipc import IpcClient, NamedPipeClientTransport

        self._client = IpcClient(
            transport=NamedPipeClientTransport(self._endpoint),
            request_timeout=None,
        )
        self._proxy = self._client.get_proxy(IIpcLogSink)
        self._queue = asyncio.Queue()
        self._consumer = asyncio.ensure_future(self._consume())

    async def _consume(self) -> None:
        assert self._queue is not None
        consecutive_drops = 0
        muted_until = 0.0
        while True:
            item = await self._queue.get()
            try:
                if item is _STOP:
                    return
                if time.monotonic() < muted_until:
                    continue
                if await self._send_log_once(item):
                    consecutive_drops = 0
                else:
                    consecutive_drops += 1
                    if consecutive_drops >= _MAX_CONSECUTIVE_DROPS:
                        muted_until = time.monotonic() + _FAILURE_COOLDOWN_S
                        consecutive_drops = 0
                        self._log.warning(
                            "Job-API IPC: log forwarding paused for %ss after "
                            "repeated send failures.",
                            int(_FAILURE_COOLDOWN_S),
                        )
            finally:
                self._queue.task_done()

    async def _send_log_once(self, dto: JobLogDto) -> bool:
        assert self._proxy is not None
        last_err: BaseException | None = None
        for attempt in range(1, _MAX_SEND_ATTEMPTS + 1):
            try:
                await asyncio.wait_for(
                    self._proxy.SendLog(self._job_id, dto), timeout=_SEND_TIMEOUT_S
                )
                return True
            except Exception as e:
                last_err = e
                if attempt < _MAX_SEND_ATTEMPTS:
                    await asyncio.sleep(_RETRY_DELAY_S)
        self._log.debug("Job-API IPC: dropped one log entry: %s", last_err)
        return False

    def send_log(self, dto: JobLogDto) -> None:
        """Enqueue one log entry for sending (thread-safe, never blocks)."""
        loop = self._loop
        queue = self._queue
        if loop is None or queue is None or self._stopping:
            return
        try:
            loop.call_soon_threadsafe(queue.put_nowait, dto)
        except RuntimeError:
            pass

    def close(self) -> None:
        """Flush queued logs, close the client, and join the loop thread."""
        self._stopping = True
        loop = self._loop
        thread = self._thread
        if loop is None or thread is None or not thread.is_alive():
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(self._shutdown(), loop)
            fut.result(timeout=_SHUTDOWN_TIMEOUT_S)
        except Exception as e:
            self._log.debug("Job-API IPC: error during shutdown: %s", e)
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=_SHUTDOWN_TIMEOUT_S)

    async def _shutdown(self) -> None:
        if self._queue is not None and self._consumer is not None:
            await self._queue.join()
            self._queue.put_nowait(_STOP)
            try:
                await asyncio.wait_for(self._consumer, timeout=_SHUTDOWN_TIMEOUT_S)
            except Exception:
                self._consumer.cancel()
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception as e:
                self._log.debug("Job-API IPC: error closing client: %s", e)
