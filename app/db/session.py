"""SQLAlchemy engine and session-factory helpers."""

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker


def create_engine_from_url(database_url: str) -> Engine:
    """Create an engine with the options required by its database backend."""

    url = make_url(database_url)
    connect_args: dict[str, bool] = {}

    if url.get_backend_name() == "sqlite":
        # FastAPI may use a connection from a worker thread different from its creator.
        connect_args["check_same_thread"] = False

    return create_engine(url, connect_args=connect_args, pool_pre_ping=True)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create sessions that retain loaded values after a successful commit."""

    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
