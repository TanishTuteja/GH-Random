"""Database-scoped exclusion for cooperating crawlers and online migrations."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.engine import Connection

# Stable protocol identifiers, not deployment settings: every cooperating version
# must contend for the same two-integer PostgreSQL advisory lock ("GHR", catalog).
CATALOG_LOCK_NAMESPACE = 0x474852
CATALOG_LOCK_RESOURCE = 1
OWNERSHIP_QUERY = f"""
SELECT EXISTS (
    SELECT 1 FROM pg_locks
    WHERE locktype = 'advisory' AND pid = pg_backend_pid() AND granted
      AND mode = 'ExclusiveLock' AND objsubid = 2
      AND classid = {CATALOG_LOCK_NAMESPACE} AND objid = {CATALOG_LOCK_RESOURCE}
)
"""


class CatalogOperationBusy(RuntimeError):
    """A cooperating crawler or migration already owns this database."""


class CatalogOwnershipLost(RuntimeError):
    """The original connection/lock was lost; this operation must not continue."""


@contextmanager
def exclusive_catalog_operation(connection: Connection) -> Iterator[None]:
    """Own one physical DB session and fence every statement/commit against loss.

    The caller must use this same Connection for its work. PostgreSQL advisory
    locks survive transaction boundaries, unlike row/transaction locks. Never
    return an owning DB session to the pool: close/invalidate it on every exit.
    A reconnected session must not continue the old operation or acquire again.
    """

    if connection.dialect.name != "postgresql":
        raise ValueError("Catalog coordination requires PostgreSQL")
    if connection.in_transaction():
        raise ValueError("Acquire catalog ownership before starting a transaction")
    guarded = False
    owner: Any = None

    def check_ownership() -> None:
        if (
            connection.closed
            or connection.invalidated
            or connection.connection.dbapi_connection is not owner
        ):
            raise CatalogOwnershipLost("Catalog operation lost its database connection")
        # A separate raw cursor avoids recursive SQLAlchemy events and also works
        # when the statement being guarded uses a server-side streaming cursor.
        cursor = owner.cursor()
        try:
            cursor.execute(OWNERSHIP_QUERY)
            row = cursor.fetchone()
            if row is None or row[0] is not True:
                raise CatalogOwnershipLost("Catalog operation no longer owns its database lock")
        finally:
            cursor.close()

    def before_statement(
        conn: Connection,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        check_ownership()

    def before_commit(conn: Connection) -> None:
        check_ownership()

    try:
        acquired = connection.scalar(
            text("SELECT pg_try_advisory_lock(:namespace, :resource)"),
            {"namespace": CATALOG_LOCK_NAMESPACE, "resource": CATALOG_LOCK_RESOURCE},
        )
        connection.commit()
        if not acquired:
            raise CatalogOperationBusy("A crawl or migration already owns this catalog database")
        owner = connection.connection.dbapi_connection
        event.listen(connection, "before_cursor_execute", before_statement)
        event.listen(connection, "commit", before_commit)
        guarded = True
        yield
    finally:
        if guarded:
            event.remove(connection, "before_cursor_execute", before_statement)
            event.remove(connection, "commit", before_commit)
        if not connection.closed:
            # Physical close releases the session lock, including on rollback,
            # KeyboardInterrupt, a broken socket, or caller failure. Pool reset
            # alone would leave a session-level advisory lock held.
            connection.invalidate()
