"""LangGraph checkpointer setup (owner-only DDL).

The graph's durable state lives in Postgres so a paused approval survives a restart. The
tables are created by the schema owner during the migrate step - the runtime role has no
DDL rights - and the runtime role inherits DML on them from the default privileges granted
in migration 0005.

Run: ``python -m opsassist.agent.checkpointer``
"""

from __future__ import annotations

import asyncio

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from opsassist.config import Settings, get_settings
from opsassist.logging_setup import configure_logging, get_logger


def psycopg_url(url: str) -> str:
    """SQLAlchemy URL -> plain libpq URL used by the checkpointer."""
    return url.replace("postgresql+psycopg://", "postgresql://")


async def setup(settings: Settings) -> None:
    async with AsyncPostgresSaver.from_conn_string(
        psycopg_url(settings.migration_database_url.get_secret_value())
    ) as saver:
        await saver.setup()


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, "opsassist-checkpointer")
    asyncio.run(setup(settings))
    get_logger("opsassist.agent").info("checkpointer_ready")


if __name__ == "__main__":
    main()
