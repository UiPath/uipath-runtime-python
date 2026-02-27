"""Tests for UiPathRuntimeLogsInterceptor teardown with non-UTF-8 stdout."""

import io
import logging
import sys
from unittest.mock import patch

import pytest

from uipath.runtime.logging._interceptor import UiPathRuntimeLogsInterceptor


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
