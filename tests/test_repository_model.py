from sqlalchemy import create_engine, inspect

from app.db import Base


def test_catalog_schema_has_repository_and_generation_tables() -> None:
    engine = create_engine("sqlite://")

    try:
        Base.metadata.create_all(engine)
        inspector = inspect(engine)

        assert {
            "repositories",
            "catalog_generations",
            "crawl_runs",
            "generation_repositories",
        } <= set(inspector.get_table_names())

        repository_columns = {column["name"] for column in inspector.get_columns("repositories")}
        assert {
            "github_id",
            "full_name",
            "language",
            "stargazer_count",
            "github_updated_at",
            "github_pushed_at",
            "last_seen_at",
            "topics",
        } <= repository_columns

        indexes = {index["name"] for index in inspector.get_indexes("repositories")}
        assert {
            "ix_repositories_language",
            "ix_repositories_stargazer_count",
            "ix_repositories_github_updated_at",
            "ix_repositories_github_pushed_at",
        } <= indexes
    finally:
        engine.dispose()
