"""Exercise the real PostgreSQL ownership protocol, not a mocked mutex."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import CatalogGeneration
from app.db.operations import (
    CatalogOperationBusy,
    CatalogOwnershipLost,
    exclusive_catalog_operation,
)


def test_exclusion_survives_commit_and_rollback_while_reads_continue(
    database_engine: Engine,
) -> None:
    with database_engine.connect() as owner, exclusive_catalog_operation(owner):
        owner.execute(text("SELECT 1"))
        owner.commit()
        owner.execute(text("SELECT 1"))
        owner.rollback()
        with database_engine.connect() as contender:
            with pytest.raises(CatalogOperationBusy):
                with exclusive_catalog_operation(contender):
                    pytest.fail("Second catalog owner was admitted")
        with database_engine.connect() as reader:
            assert reader.scalar(select(func.count()).select_from(CatalogGeneration)) == 0
    with database_engine.connect() as next_owner, exclusive_catalog_operation(next_owner):
        assert next_owner.scalar(text("SELECT 1")) == 1


def test_exception_does_not_leave_a_lock_in_the_pool(database_engine: Engine) -> None:
    with pytest.raises(RuntimeError, match="interrupted"):
        with database_engine.connect() as connection, exclusive_catalog_operation(connection):
            raise RuntimeError("interrupted")
    with database_engine.connect() as connection, exclusive_catalog_operation(connection):
        assert connection.scalar(text("SELECT 1")) == 1


def test_cannot_reconnect_and_continue_an_old_operation(database_engine: Engine) -> None:
    with database_engine.connect() as connection, exclusive_catalog_operation(connection):
        connection.invalidate()
        with pytest.raises(CatalogOwnershipLost):
            connection.execute(text("SELECT 1"))
    with database_engine.connect() as connection, exclusive_catalog_operation(connection):
        assert connection.scalar(text("SELECT 1")) == 1


def test_lost_lock_fences_statements_and_commit(database_engine: Engine) -> None:
    with database_engine.connect() as connection, exclusive_catalog_operation(connection):
        connection.execute(text("SELECT pg_advisory_unlock_all()"))
        with pytest.raises(CatalogOwnershipLost):
            connection.execute(text("SELECT 1"))
        with pytest.raises(CatalogOwnershipLost):
            connection.commit()


def test_lost_ownership_cannot_commit_pending_writes(database_engine: Engine) -> None:
    with database_engine.connect() as connection, exclusive_catalog_operation(connection):
        connection.execute(
            text("INSERT INTO catalog_generations (status,created_at) VALUES ('staging',now())")
        )
        connection.execute(text("SELECT pg_advisory_unlock_all()"))
        with pytest.raises(CatalogOwnershipLost):
            connection.commit()
    with database_engine.connect() as reader:
        assert reader.scalar(select(func.count()).select_from(CatalogGeneration)) == 0


def test_factory_uses_one_owning_connection_and_commits_checkpoints(
    database_engine: Engine,
) -> None:
    with database_engine.connect() as connection, exclusive_catalog_operation(connection):
        factory = sessionmaker(bind=connection)
        with factory() as session, session.begin():
            session.execute(
                text("INSERT INTO catalog_generations (status,created_at) VALUES ('staging',now())")
            )
        with Session(database_engine) as reader:
            assert reader.scalar(select(func.count()).select_from(CatalogGeneration)) == 1
        with factory() as session, session.begin():
            assert session.scalar(select(func.count()).select_from(CatalogGeneration)) == 1


def test_rejects_an_existing_transaction(database_engine: Engine) -> None:
    with database_engine.connect() as connection:
        connection.execute(text("SELECT 1"))
        with pytest.raises(ValueError, match="before starting a transaction"):
            with exclusive_catalog_operation(connection):
                pytest.fail("Should not acquire inside a pre-existing transaction")


def test_backend_termination_does_not_allow_owner_to_continue(database_engine: Engine) -> None:
    with database_engine.connect() as owner, exclusive_catalog_operation(owner):
        backend_pid = owner.scalar(text("SELECT pg_backend_pid()"))
        owner.commit()
        with database_engine.begin() as admin:
            assert admin.scalar(text("SELECT pg_terminate_backend(:pid)"), {"pid": backend_pid})
        with pytest.raises(OperationalError):
            owner.execute(text("SELECT 1"))
    with database_engine.connect() as successor, exclusive_catalog_operation(successor):
        assert successor.scalar(text("SELECT 1")) == 1
