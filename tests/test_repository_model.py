from sqlalchemy import create_engine, inspect

from app.db import Base


def test_repository_catalog_table_has_filter_columns() -> None:
    engine = create_engine("sqlite://")

    try:
        Base.metadata.create_all(engine)
        inspector = inspect(engine)

        assert inspector.get_table_names() == ["repositories"]

        columns = {column["name"] for column in inspector.get_columns("repositories")}
        assert {
            "github_id",
            "full_name",
            "language",
            "stargazer_count",
            "github_updated_at",
            "last_crawled_at",
        } <= columns

        indexes = {index["name"] for index in inspector.get_indexes("repositories")}
        assert {
            "ix_repositories_language",
            "ix_repositories_stargazer_count",
            "ix_repositories_github_updated_at",
        } <= indexes
    finally:
        engine.dispose()
