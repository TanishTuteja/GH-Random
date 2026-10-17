"""Run the real Alembic entrypoint against a contended disposable database."""

from __future__ import annotations

import os
import subprocess
import sys

from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from app.db.operations import exclusive_catalog_operation


def test_online_migration_fails_before_ddl_while_catalog_is_owned(database_engine: Engine) -> None:
    before = inspect(database_engine).get_table_names()
    with database_engine.connect() as connection, exclusive_catalog_operation(connection):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            env={
                **os.environ,
                "DATABASE_URL": database_engine.url.render_as_string(hide_password=False),
            },
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.returncode != 0
        assert "CatalogOperationBusy" in result.stderr
    assert inspect(database_engine).get_table_names() == before
