"""Alembic environment of the "s" edition's own tables.

Runs after upstream's chain (docker-entrypoint.sh), against the same database,
with its own version table: upstream's revisions and these never share a
history, so neither can collide with the other.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from src.core.config import AppConfig
from src.infrastructure.database.models.s import SBase

VERSION_TABLE = "alembic_version_s"

config = context.config
app_config = AppConfig.get()

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = SBase.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=app_config.database.dsn,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table=VERSION_TABLE,
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        version_table=VERSION_TABLE,
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable: AsyncEngine = create_async_engine(url=app_config.database.dsn)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
