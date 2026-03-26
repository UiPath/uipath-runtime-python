"""Tests for UiPathRuntimeLogsInterceptor."""

import asyncio
import io
import logging
import sys
from unittest.mock import patch

import pytest

from uipath.runtime.logging._context import current_execution_id
from uipath.runtime.logging._interceptor import UiPathRuntimeLogsInterceptor
from uipath.runtime.logging._writers import LoggerWriter
from uipath.runtime.logging.handlers import UiPathRuntimeExecutionLogHandler


@pytest.fixture(autouse=True)
def _isolate_logging():
    """Save and restore logging state so tests don't leak into each other."""
    root = logging.getLogger()
    original_level = root.level
    original_handlers = list(root.handlers)
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    yield
    root.setLevel(original_level)
    root.handlers = original_handlers
    sys.stdout = original_stdout
    sys.stderr = original_stderr
    logging.disable(logging.NOTSET)


def _make_cp1252_stdout() -> io.TextIOWrapper:
    """Create a TextIOWrapper that mimics Windows cp1252 piped stdout."""
    raw_buffer = io.BytesIO()
    return io.TextIOWrapper(raw_buffer, encoding="cp1252", line_buffering=True)


class TestInterceptorTeardownPreservesBuffer:
    """Verify that teardown does not destroy the underlying stdout buffer."""

    def test_buffer_usable_after_teardown(self):
        """After setup+teardown the original stdout buffer must still be writable."""
        fake_stdout = _make_cp1252_stdout()

        with (
            patch.object(sys, "stdout", fake_stdout),
            patch.object(sys, "stderr", fake_stdout),
        ):
            interceptor = UiPathRuntimeLogsInterceptor(job_id=None)

            # The wrapper should have been created because encoding is cp1252
            assert hasattr(interceptor, "utf8_stdout")

            interceptor.setup()
            interceptor.teardown()

        # The underlying buffer must still be open and writable
        assert not fake_stdout.buffer.closed
        fake_stdout.buffer.write(b"still alive")

    def test_no_valueerror_writing_after_teardown(self):
        """Writing to the original stdout after teardown must not raise ValueError."""
        fake_stdout = _make_cp1252_stdout()

        with (
            patch.object(sys, "stdout", fake_stdout),
            patch.object(sys, "stderr", fake_stdout),
        ):
            interceptor = UiPathRuntimeLogsInterceptor(job_id=None)
            interceptor.setup()
            interceptor.teardown()

        # This simulates what click.echo() does — write to the restored stdout
        fake_stdout.write("no crash")
        fake_stdout.flush()

    def test_utf8_stdout_not_created_for_utf8_encoding(self):
        """When stdout is already UTF-8, no wrapper should be created."""
        utf8_stdout = io.TextIOWrapper(
            io.BytesIO(), encoding="utf-8", line_buffering=True
        )

        with (
            patch.object(sys, "stdout", utf8_stdout),
            patch.object(sys, "stderr", utf8_stdout),
        ):
            interceptor = UiPathRuntimeLogsInterceptor(job_id=None)

        assert not hasattr(interceptor, "utf8_stdout")

    def test_utf8_stdout_attr_removed_after_teardown(self):
        """After teardown, the utf8_stdout attribute should be deleted (double-teardown guard)."""
        fake_stdout = _make_cp1252_stdout()

        with (
            patch.object(sys, "stdout", fake_stdout),
            patch.object(sys, "stderr", fake_stdout),
        ):
            interceptor = UiPathRuntimeLogsInterceptor(job_id=None)
            interceptor.setup()
            interceptor.teardown()

        assert not hasattr(interceptor, "utf8_stdout")

    def test_double_teardown_does_not_raise(self):
        """Calling teardown twice must not raise (guarded by del)."""
        fake_stdout = _make_cp1252_stdout()

        with (
            patch.object(sys, "stdout", fake_stdout),
            patch.object(sys, "stderr", fake_stdout),
        ):
            interceptor = UiPathRuntimeLogsInterceptor(job_id=None)
            interceptor.setup()
            interceptor.teardown()
            # Second teardown should be safe
            interceptor.teardown()


class TestInterceptorTeardownOrder:
    """Verify that detach happens before handler close."""

    def test_detach_called_before_handler_close(self):
        """utf8_stdout.detach() must execute before log_handler.close()."""
        fake_stdout = _make_cp1252_stdout()
        call_order: list[str] = []

        with (
            patch.object(sys, "stdout", fake_stdout),
            patch.object(sys, "stderr", fake_stdout),
        ):
            interceptor = UiPathRuntimeLogsInterceptor(job_id=None)
            assert hasattr(interceptor, "utf8_stdout")

            # Wrap detach and close to record call order
            original_detach = interceptor.utf8_stdout.detach
            original_close = interceptor.log_handler.close

            def tracked_detach():
                call_order.append("detach")
                return original_detach()

            def tracked_close():
                call_order.append("handler_close")
                return original_close()

            with (
                patch.object(interceptor.utf8_stdout, "detach", tracked_detach),
                patch.object(interceptor.log_handler, "close", tracked_close),
            ):
                interceptor.setup()
                interceptor.teardown()

        assert "detach" in call_order
        assert "handler_close" in call_order
        assert call_order.index("detach") < call_order.index("handler_close")


class TestInterceptorWithJobId:
    """When job_id is set, a file handler is used — no utf8_stdout wrapper."""

    def test_no_utf8_wrapper_with_job_id(self, tmp_path):
        """File-based handler path should never create utf8_stdout."""
        fake_stdout = _make_cp1252_stdout()

        with (
            patch.object(sys, "stdout", fake_stdout),
            patch.object(sys, "stderr", fake_stdout),
        ):
            interceptor = UiPathRuntimeLogsInterceptor(
                job_id="job-123", dir=str(tmp_path), file="test.log"
            )

        assert not hasattr(interceptor, "utf8_stdout")


class TestLoggerWriterPerContextBuffers:
    """Verify LoggerWriter isolates buffers by execution context."""

    def _make_writer(self) -> tuple[LoggerWriter, logging.Logger, list[logging.LogRecord]]:
        logger = logging.getLogger("test_writer")
        logger.handlers.clear()
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        records: list[logging.LogRecord] = []
        handler = logging.Handler()
        handler.emit = lambda r: records.append(r)
        logger.addHandler(handler)
        writer = LoggerWriter(logger, logging.INFO, logging.INFO, sys.__stdout__)
        return writer, logger, records

    def test_no_context_uses_none_key(self):
        """Writes without execution context use None as buffer key."""
        writer, _, records = self._make_writer()

        writer.write("hello\n")
        assert None not in writer._buffers  # fully flushed line, no leftover
        assert len(records) == 1

        writer.write("partial")
        assert writer._buffers.get(None) == "partial"

    def test_separate_buffers_per_context(self):
        """Different execution contexts get independent buffers."""
        writer, _, _ = self._make_writer()

        current_execution_id.set("exec-1")
        writer.write("from exec 1")

        current_execution_id.set("exec-2")
        writer.write("from exec 2")

        assert writer._buffers.get("exec-1") == "from exec 1"
        assert writer._buffers.get("exec-2") == "from exec 2"

        current_execution_id.set(None)

    def test_flush_only_current_context(self):
        """flush() only flushes the current context's buffer."""
        writer, _, records = self._make_writer()

        current_execution_id.set("exec-1")
        writer.write("line 1 partial")

        current_execution_id.set("exec-2")
        writer.write("line 2 partial")

        # Flush only exec-2
        writer.flush()

        assert "exec-2" not in writer._buffers
        assert writer._buffers.get("exec-1") == "line 1 partial"
        assert len(records) == 1
        assert records[0].getMessage() == "line 2 partial"

        current_execution_id.set(None)

    def test_flush_all_flushes_every_context(self):
        """flush_all() flushes all contexts."""
        writer, _, records = self._make_writer()

        current_execution_id.set("exec-1")
        writer.write("partial 1")

        current_execution_id.set("exec-2")
        writer.write("partial 2")

        writer.flush_all()

        assert len(writer._buffers) == 0
        messages = {r.getMessage() for r in records}
        assert messages == {"partial 1", "partial 2"}

        current_execution_id.set(None)

    def test_complete_lines_emitted_immediately(self):
        """Lines ending with newline are emitted, not buffered."""
        writer, _, records = self._make_writer()

        writer.write("complete line\n")
        assert len(records) == 1
        assert records[0].getMessage() == "complete line"
        assert len(writer._buffers) == 0


class TestChildInterceptorNoStreamReplace:
    """Verify child interceptors don't replace sys.stdout/sys.stderr."""

    def test_child_does_not_replace_stdout(self, tmp_path):
        """Child interceptor must not overwrite sys.stdout."""
        master = UiPathRuntimeLogsInterceptor(
            job_id="job-1", dir=str(tmp_path), file="test.log"
        )
        master.setup()

        master_stdout = sys.stdout
        assert isinstance(master_stdout, LoggerWriter)

        child_handler = UiPathRuntimeExecutionLogHandler("exec-1")
        child = UiPathRuntimeLogsInterceptor(
            execution_id="exec-1", log_handler=child_handler
        )
        child.setup()

        # sys.stdout must still be the master's LoggerWriter
        assert sys.stdout is master_stdout

        child.teardown()
        master.teardown()

    def test_child_registers_handler_on_stdout_logger(self, tmp_path):
        """Child interceptor must register its handler on the stdout logger."""
        master = UiPathRuntimeLogsInterceptor(
            job_id="job-1", dir=str(tmp_path), file="test.log"
        )
        master.setup()

        child_handler = UiPathRuntimeExecutionLogHandler("exec-1")
        child = UiPathRuntimeLogsInterceptor(
            execution_id="exec-1", log_handler=child_handler
        )
        child.setup()

        stdout_logger = logging.getLogger("stdout")
        assert child_handler in stdout_logger.handlers

        child.teardown()

        # After child teardown, handler should be removed
        assert child_handler not in stdout_logger.handlers

        master.teardown()


class TestTeardownFlushOrder:
    """Verify partial lines are flushed before teardown completes."""

    def test_child_flushes_partial_line_on_teardown(self, tmp_path):
        """Partial line in LoggerWriter buffer must be captured on child teardown."""
        master = UiPathRuntimeLogsInterceptor(
            job_id="job-1", dir=str(tmp_path), file="test.log"
        )
        master.setup()

        child_handler = UiPathRuntimeExecutionLogHandler("exec-1")
        child = UiPathRuntimeLogsInterceptor(
            execution_id="exec-1", log_handler=child_handler
        )
        child.setup()

        # Write a partial line (no newline) via print
        print("partial line no newline", end="")

        child.teardown()

        # The partial line should have been flushed to the child's handler
        messages = [child_handler.formatter.format(r) for r in child_handler.buffer]
        assert any("partial line no newline" in m for m in messages)

        master.teardown()

    def test_master_flushes_all_on_teardown(self, tmp_path):
        """Master teardown flushes all remaining buffers."""
        master = UiPathRuntimeLogsInterceptor(
            job_id="job-1", dir=str(tmp_path), file="test.log"
        )
        master.setup()

        # Write a partial line in master context (no execution_id)
        print("master partial", end="")

        master.teardown()

        # Read the log file to verify the partial line was written
        log_file = tmp_path / "test.log"
        log_content = log_file.read_text()
        assert "master partial" in log_content


class TestParallelExecutionLogIsolation:
    """End-to-end test: parallel async tasks produce correctly separated logs."""

    @pytest.mark.asyncio
    async def test_parallel_tasks_isolated(self, tmp_path):
        """Concurrent eval executions must not interleave log output."""
        master = UiPathRuntimeLogsInterceptor(
            job_id="job-1", dir=str(tmp_path), file="test.log"
        )
        master.setup()

        child_handlers: dict[str, UiPathRuntimeExecutionLogHandler] = {}

        async def run_execution(exec_id: str) -> None:
            handler = UiPathRuntimeExecutionLogHandler(exec_id)
            child_handlers[exec_id] = handler

            child = UiPathRuntimeLogsInterceptor(
                execution_id=exec_id, log_handler=handler
            )
            child.setup()

            try:
                # Mix logging and print calls with yields between them
                for i in range(5):
                    logging.info(f"log from {exec_id} iter {i}")
                    print(f"print from {exec_id} iter {i}")
                    await asyncio.sleep(0)  # yield to other tasks
            finally:
                child.teardown()

        # Run 4 concurrent executions
        exec_ids = [f"exec-{i}" for i in range(4)]
        await asyncio.gather(*(run_execution(eid) for eid in exec_ids))

        master.teardown()

        # Verify each handler only contains its own messages
        for exec_id in exec_ids:
            handler = child_handlers[exec_id]
            messages = [handler.formatter.format(r) for r in handler.buffer]
            for msg in messages:
                assert exec_id in msg, (
                    f"Handler for {exec_id} contains foreign message: {msg}"
                )
            # Each execution should have captured both logging and print output
            # 5 logging.info + 5 print = 10 messages
            assert len(messages) == 10, (
                f"Handler for {exec_id} has {len(messages)} messages, expected 10"
            )
