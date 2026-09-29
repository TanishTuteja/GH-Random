"""SQLAlchemy engine and session-factory helpers."""

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker


def create_engine_from_url(database_url: str) -> Engine:
    """Create an engine for the PostgreSQL-only application database."""

    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        raise ValueError("DATABASE_URL must use the PostgreSQL backend.")

    return create_engine(url, pool_pre_ping=True)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create sessions that retain loaded values after a successful commit."""

    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
