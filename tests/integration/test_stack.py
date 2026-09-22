"""Runs against the live compose stack (`make up`). Skipped by `make test`."""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text

from opsassist.config import get_settings
from opsassist.db.session import create_engine
from opsassist.seed import seed

pytestmark = pytest.mark.integration
API = os.environ.get("OPSASSIST_API_URL", "http://127.0.0.1:8000")


def test_api_is_ready_against_real_dependencies() -> None:
    resp = httpx.get(f"{API}/readyz", timeout=5)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ready"


async def test_seed_is_idempotent(sample_data_dir: Path) -> None:
    await seed(sample_data_dir)
    await seed(sample_data_dir)
    engine = create_engine(get_settings())
    try:
        async with engine.connect() as conn:
            assert await conn.scalar(text("SELECT count(*) FROM users")) == 6
            assert await conn.scalar(text("SELECT count(*) FROM servers")) == 4
    finally:
        await engine.dispose()
