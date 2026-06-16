import sys
import tempfile
from pathlib import Path
from typing import Generator

import pytest

# Ensure local source package (src/uipath) is importable before tests collect
_PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]
_SRC_PATH: Path = _PROJECT_ROOT / "src"
if _SRC_PATH.exists():
    sys.path.insert(0, str(_SRC_PATH))


@pytest.fixture
def temp_dir() -> Generator[str, None, None]:
    """Provide a temporary directory for test files."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        yield tmp_dir


@pytest.fixture(autouse=True)
def _reset_governance_process_state() -> Generator[None, None, None]:
    """Clear process-level governance state around every test.

    The native governance layer keeps two pieces of state at module scope:
    the conversational/autonomous selector consumed by the policy fetch,
    and the memoized job-context. Both are stable per process in
    production but leak across tests when not reset, masking ordering
    bugs and producing flakes.

    ``backend_client`` is imported lazily and guarded: this shared
    conftest ships alongside the foundation slice, where that module may
    not exist yet, and the reset is simply a no-op until it does.
    """
    try:
        from uipath.runtime.governance.native.backend_client import (
            resolve_job_context,
            set_agent_conversational,
        )
    except ImportError:
        yield
        return

    set_agent_conversational(None)
    resolve_job_context.cache_clear()
    yield
    set_agent_conversational(None)
    resolve_job_context.cache_clear()
