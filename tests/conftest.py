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


# Governance state — provider, conversational selector, policy cache,
# enforcement mode — is owned by each :class:`PolicyLoader` instance,
# so no autouse cross-test reset is needed. Tests that want a clean
# slate just construct a fresh loader.
