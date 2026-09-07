import asyncio
import logging
import time
import uuid

import pytest

import uipath.runtime.jobapi.client as client_mod
from uipath.runtime.jobapi.client import IpcJobApiClient
from uipath.runtime.jobapi.contract import IIpcLogSink, JobLogDto


def test_start_without_uipath_ipc_raises_loudly(monkeypatch):
    # A missing transport must fail, not silently skip: the handler has already
    # stopped reading the execution.log this client replaces.
    monkeypatch.setattr(
        "uipath.runtime.jobapi.client.importlib.util.find_spec",
        lambda name: None,
    )
    client = IpcJobApiClient("pipe", "job", logging.getLogger("test"))
    with pytest.raises(RuntimeError, match="uipath-ipc"):
        client.start()


async def test_roundtrip_streams_logs_and_flushes_on_close():
    pytest.importorskip("uipath_ipc")
    from uipath_ipc import IpcServer, NamedPipeServerTransport

    pipe = f"uipath-jobapi-test-{uuid.uuid4().hex}"
    received: list[tuple[object, object]] = []

    class _FakeHandler(IIpcLogSink):
        async def SendLog(self, jobId, log):
            received.append((jobId, log))

    server = IpcServer(
        transport=NamedPipeServerTransport(pipe),
        services={IIpcLogSink: _FakeHandler()},
        request_timeout=None,
    )
    async with server:
        serve = asyncio.ensure_future(server.serve_forever())
        try:
            job_id = str(uuid.uuid4())
            client = IpcJobApiClient(pipe, job_id, logging.getLogger("test"))
            await asyncio.to_thread(client.start)
            client.send_log(JobLogDto(Message="first", LogLevel=2))
            client.send_log(JobLogDto(Message="second", LogLevel=3))
            # close() flushes the queue before tearing the channel down.
            await asyncio.to_thread(client.close)
        finally:
            serve.cancel()

    def _field(obj, name):
        return getattr(obj, name) if hasattr(obj, name) else obj[name]

    assert [
        (_field(log, "Message"), _field(log, "LogLevel")) for _, log in received
    ] == [
        ("first", 2),
        ("second", 3),
    ]
    assert all(job == job_id for job, _ in received)


def test_start_times_out_when_thread_never_ready(monkeypatch):
    monkeypatch.setattr(
        "uipath.runtime.jobapi.client.importlib.util.find_spec",
        lambda name: object(),
    )
    monkeypatch.setattr(client_mod, "_STARTUP_TIMEOUT_S", 0.1)
    client = IpcJobApiClient("pipe", "job", logging.getLogger("test"))
    monkeypatch.setattr(client, "_run", lambda: time.sleep(2))
    with pytest.raises(RuntimeError, match="did not connect"):
        client.start()


def test_start_raises_when_setup_fails(monkeypatch):
    monkeypatch.setattr(
        "uipath.runtime.jobapi.client.importlib.util.find_spec",
        lambda name: object(),
    )
    client = IpcJobApiClient("pipe", "job", logging.getLogger("test"))

    async def _boom():
        raise RuntimeError("setup boom")

    monkeypatch.setattr(client, "_setup", _boom)
    with pytest.raises(RuntimeError, match="setup boom"):
        client.start()


async def test_send_log_once_retries_then_drops(monkeypatch):
    monkeypatch.setattr(client_mod, "_RETRY_DELAY_S", 0)
    client = IpcJobApiClient("pipe", "job", logging.getLogger("test"))

    class _Boom(IIpcLogSink):
        async def SendLog(self, jobId, log):
            raise RuntimeError("nope")

    client._proxy = _Boom()
    assert await client._send_log_once(JobLogDto(Message="m")) is False


async def test_consume_mutes_after_repeated_failures(monkeypatch):
    monkeypatch.setattr(client_mod, "_RETRY_DELAY_S", 0)
    client = IpcJobApiClient("pipe", "job", logging.getLogger("test"))
    sends = 0

    class _Boom(IIpcLogSink):
        async def SendLog(self, jobId, log):
            nonlocal sends
            sends += 1
            raise RuntimeError("x")

    client._proxy = _Boom()
    client._queue = asyncio.Queue()
    task = asyncio.ensure_future(client._consume())

    for i in range(client_mod._MAX_CONSECUTIVE_DROPS):
        client._queue.put_nowait(JobLogDto(Message=str(i)))
    await client._queue.join()
    sends_while_trying = sends

    client._queue.put_nowait(JobLogDto(Message="muted"))
    await client._queue.join()
    assert sends == sends_while_trying  # dropped while muted, no send attempted

    client._queue.put_nowait(client_mod._STOP)
    await task


def test_send_log_noop_before_start():
    client = IpcJobApiClient("pipe", "job", logging.getLogger("test"))
    client.send_log(JobLogDto(Message="x"))


async def test_send_log_swallows_closed_loop_error():
    client = IpcJobApiClient("pipe", "job", logging.getLogger("test"))

    class _DeadLoop:
        def call_soon_threadsafe(self, *args):
            raise RuntimeError("loop closed")

    client._loop = _DeadLoop()  # type: ignore[assignment]
    client._queue = asyncio.Queue()
    client.send_log(JobLogDto(Message="x"))


def test_close_noop_before_start():
    client = IpcJobApiClient("pipe", "job", logging.getLogger("test"))
    client.close()
