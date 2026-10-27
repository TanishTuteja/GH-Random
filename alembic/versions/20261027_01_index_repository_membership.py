"""Index repository membership lookups for retention cleanup."""

from collections.abc import Sequence

from alembic import op

# Preserve the published baseline; existing databases upgrade by adding an index.
revision: str = "20261027_01"
down_revision: str | None = "20260930_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Avoid rescanning retained memberships for every orphan deletion."""

    op.create_index(
        "ix_generation_repositories_github_id",
        "generation_repositories",
        ["github_id"],
        unique=False,
    )


def downgrade() -> None:
    """Remove only the added lookup index, preserving catalog data."""

    op.drop_index("ix_generation_repositories_github_id", table_name="generation_repositories")
