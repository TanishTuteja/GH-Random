from sqlalchemy import inspect
from sqlalchemy.engine import Engine


def test_catalog_schema_has_repository_and_generation_tables(database_engine: Engine) -> None:
    inspector = inspect(database_engine)

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


def test_repository_memberships_have_a_repository_lookup_index(database_engine: Engine) -> None:
    # The generation-first composite PK does not efficiently support FK cleanup
    # by repository ID. Inspect the actual PostgreSQL schema, not just metadata.
    indexes = inspect(database_engine).get_indexes("generation_repositories")
    assert any(index["column_names"] == ["github_id"] for index in indexes)
