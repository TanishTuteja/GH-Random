from logging.config import fileConfig

from alembic import context
from app.core.settings import get_settings
from app.db import Base
from app.db.operations import exclusive_catalog_operation
from app.db.session import create_engine_from_url

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations without creating a database connection."""

    context.configure(
        url=get_settings().database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against the configured database."""

    supplied = config.attributes.get("catalog_connection")
    if supplied is not None:
        check = config.attributes.get("catalog_ownership_check")
        if not callable(check):
            raise RuntimeError("A supplied migration connection requires catalog ownership")
        context.configure(connection=supplied, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            check()
            context.run_migrations()
        return

    connectable = create_engine_from_url(get_settings().database_url)

    try:
        with connectable.connect() as connection, exclusive_catalog_operation(connection):
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
            )

            with context.begin_transaction():
                context.run_migrations()
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
