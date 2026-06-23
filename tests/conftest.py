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

    The loader keeps the conversational selector and the registered
    policy provider at module scope. Both are stable per process in
    production but leak across tests when not reset, masking ordering
    bugs and producing flakes. Import is guarded so this fixture is a
    no-op when the governance package isn't built yet.
    """
    try:
        from uipath.runtime.governance.native.loader import (
            set_agent_conversational,
            set_policy_provider,
        )
    except ImportError:
        yield
        return

    set_agent_conversational(None)
    set_policy_provider(None)
    yield
    set_agent_conversational(None)
    set_policy_provider(None)
