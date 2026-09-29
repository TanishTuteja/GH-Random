"""PostgreSQL fixtures shared by persistence and web tests."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from dotenv import load_dotenv
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.db.session import create_engine_from_url, create_session_factory

load_dotenv()


@pytest.fixture
def database_engine() -> Iterator[Engine]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        raise RuntimeError("Set TEST_DATABASE_URL before running the PostgreSQL test suite.")

    engine = create_engine_from_url(database_url)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def session_factory(database_engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(database_engine)
