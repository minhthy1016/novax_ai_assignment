from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def sample_data_dir() -> Path:
    return REPO_ROOT / "sample_data"
