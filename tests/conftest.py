import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import run_cli  # noqa: E402


@pytest.fixture()
def cli():
    return run_cli
