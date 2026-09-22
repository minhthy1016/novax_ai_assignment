"""Load identity and inventory fixtures into the database.

Idempotent: re-running upserts by primary key, so ``make seed`` is safe to repeat.
Fixtures are validated with Pydantic before touching the database, so a malformed
fixture fails loudly instead of seeding partial state.

Usage: ``python -m opsassist.seed [--data-dir sample_data]``
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, TypeAdapter
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from opsassist.config import get_settings
from opsassist.db.models import Base, Department, Server, User
from opsassist.db.session import create_engine, create_session_factory
from opsassist.logging_setup import configure_logging, get_logger

PERMISSION_PATTERN = re.compile(r"^[a-z_]+:[a-z_]+$")


class DepartmentFixture(BaseModel):
    slug: str = Field(pattern=r"^[a-z][a-z_]*$")
    name: str


class UserFixture(BaseModel):
    id: str = Field(pattern=r"^U[0-9]{3,}$")
    name: str
    department: str
    role: str
    permissions: list[str]

    def model_post_init(self, _context: object) -> None:
        bad = [p for p in self.permissions if not PERMISSION_PATTERN.fullmatch(p)]
        if bad:
            raise ValueError(f"user {self.id}: malformed permissions {bad}")


class ServerFixture(BaseModel):
    id: str
    environment: Literal["production", "staging", "development"]
    owner_department: str
    status: Literal["healthy", "degraded", "offline"]
    cpu_pct: Decimal | None = Field(default=None, ge=0, le=100)
    memory_pct: Decimal | None = Field(default=None, ge=0, le=100)
    last_check: datetime


class Fixtures(BaseModel):
    departments: list[DepartmentFixture]
    users: list[UserFixture]
    servers: list[ServerFixture]

    def model_post_init(self, _context: object) -> None:
        known = {d.slug for d in self.departments}
        referenced = {u.department for u in self.users} | {s.owner_department for s in self.servers}
        if missing := referenced - known:
            raise ValueError(f"fixtures reference unknown departments: {sorted(missing)}")


def load_fixtures(data_dir: Path) -> Fixtures:
    def read(name: str) -> object:
        return json.loads((data_dir / name).read_text(encoding="utf-8"))

    return Fixtures(
        departments=TypeAdapter(list[DepartmentFixture]).validate_python(read("departments.json")),
        users=TypeAdapter(list[UserFixture]).validate_python(read("users.json")),
        servers=TypeAdapter(list[ServerFixture]).validate_python(read("servers.json")),
    )


async def _upsert(session: AsyncSession, model: type[Base], rows: list[dict[str, object]]) -> None:
    if not rows:
        return
    stmt = insert(model).values(rows)
    pk = [c.name for c in inspect(model).primary_key]
    update_cols = {c: stmt.excluded[c] for c in rows[0] if c not in pk}
    await session.execute(stmt.on_conflict_do_update(index_elements=pk, set_=update_cols))


async def seed(data_dir: Path) -> Fixtures:
    fixtures = load_fixtures(data_dir)
    settings = get_settings()
    # Seeding is an owner operation, like migrations.
    engine = create_engine(
        settings.model_copy(update={"database_url": settings.migration_database_url})
    )
    try:
        async with create_session_factory(engine)() as session, session.begin():
            await _upsert(session, Department, [d.model_dump() for d in fixtures.departments])
            await _upsert(session, User, [u.model_dump() for u in fixtures.users])
            await _upsert(session, Server, [s.model_dump() for s in fixtures.servers])
    finally:
        await engine.dispose()
    return fixtures


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("sample_data"))
    args = parser.parse_args()
    configure_logging(get_settings().log_level, "opsassist-seed")
    fixtures = asyncio.run(seed(args.data_dir))
    get_logger("opsassist.seed").info(
        "seed_complete",
        departments=len(fixtures.departments),
        users=len(fixtures.users),
        servers=len(fixtures.servers),
    )


if __name__ == "__main__":
    main()
