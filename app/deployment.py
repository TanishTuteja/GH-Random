"""Private stdio deployment coordinator; never a web endpoint or Docker-socket client."""

from __future__ import annotations

import argparse
import json
import select
import sys
from collections.abc import Callable
from contextlib import redirect_stdout
from typing import Any

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import Connection

from alembic import command
from app.core.settings import get_settings
from app.db.operations import exclusive_catalog_operation
from app.db.session import create_engine_from_url


def inspect_schema(connection: Connection) -> dict[str, Any]:
    config = Config("alembic.ini")
    head = ScriptDirectory.from_config(config).get_current_head()
    revisions = MigrationContext.configure(connection).get_current_heads()
    if len(revisions) > 1:
        raise RuntimeError("Multiple database revisions require manual migration review")
    return {"revision": revisions[0] if revisions else None, "head": head}


def migrate(connection: Connection, check: Callable[[], None]) -> None:
    config = Config("alembic.ini", stdout=sys.stderr)
    config.attributes.update(catalog_connection=connection, catalog_ownership_check=check)
    command.upgrade(config, "head")
    command.check(config)


def respond(payload: dict[str, Any]) -> None:
    print(json.dumps(payload), flush=True)


def coordinate(timeout: int, *, idle_timeout: int | None = None) -> None:
    engine = create_engine_from_url(get_settings().database_url)
    try:
        with engine.connect() as connection, exclusive_catalog_operation(connection) as check:
            connection.execute(
                text("SELECT set_config('statement_timeout', :timeout, false)"),
                {"timeout": str(timeout * 1000)},
            )
            connection.commit()
            initial = inspect_schema(connection)
            connection.commit()
            respond({"ok": True, "protocol": 1, **initial})
            migrated = False
            while True:
                # EOF or idle expiry also releases ownership if the host dies.
                ready, _, _ = select.select([sys.stdin], [], [], idle_timeout or timeout)
                if not ready:
                    raise TimeoutError("Deployment controller went idle")
                line = sys.stdin.readline(1024)
                if not line:
                    return
                request = json.loads(line)
                operation = request.get("command")
                check()
                if operation == "inspect":
                    result = inspect_schema(connection)
                    connection.commit()
                    respond({"ok": True, **result})
                elif operation == "migrate" and not migrated:
                    # The same physical connection owns the lock and executes DDL.
                    connection.rollback()
                    with redirect_stdout(sys.stderr):
                        migrate(connection, check)
                    migrated = True
                    result = inspect_schema(connection)
                    connection.commit()
                    respond({"ok": True, **result})
                elif operation == "finish":
                    connection.rollback()
                    respond({"ok": True})
                    return
                else:
                    raise ValueError("Unsupported or repeated deployment command")
    finally:
        engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--idle-timeout-seconds", type=int)
    arguments = parser.parse_args()
    if arguments.timeout_seconds <= 0:
        parser.error("Timeout must be positive")
    if arguments.idle_timeout_seconds is not None and arguments.idle_timeout_seconds <= 0:
        parser.error("Idle timeout must be positive")
    try:
        coordinate(arguments.timeout_seconds, idle_timeout=arguments.idle_timeout_seconds)
    except Exception as error:
        # Never send SQL, credentials, or tracebacks through the host protocol.
        respond({"ok": False, "error": type(error).__name__})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
