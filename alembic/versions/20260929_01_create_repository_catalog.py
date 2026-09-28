"""Create the repository catalog."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260929_01"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the table that holds the latest repository snapshots."""

    op.create_table(
        "repositories",
        sa.Column("github_id", sa.BigInteger(), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("owner_login", sa.String(length=255), nullable=False),
        sa.Column("html_url", sa.String(length=2048), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("language", sa.String(length=100), nullable=True),
        sa.Column("stargazer_count", sa.Integer(), nullable=False),
        sa.Column("fork_count", sa.Integer(), nullable=False),
        sa.Column("is_archived", sa.Boolean(), nullable=False),
        sa.Column("is_fork", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("github_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_crawled_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "stargazer_count >= 0",
            name="ck_repositories_stargazer_count_nonnegative",
        ),
        sa.CheckConstraint("fork_count >= 0", name="ck_repositories_fork_count_nonnegative"),
        sa.PrimaryKeyConstraint("github_id"),
        sa.UniqueConstraint("full_name"),
    )
    op.create_index("ix_repositories_language", "repositories", ["language"], unique=False)
    op.create_index(
        "ix_repositories_stargazer_count",
        "repositories",
        ["stargazer_count"],
        unique=False,
    )
    op.create_index(
        "ix_repositories_github_updated_at",
        "repositories",
        ["github_updated_at"],
        unique=False,
    )


def downgrade() -> None:
    """Remove the repository catalog."""

    op.drop_index("ix_repositories_github_updated_at", table_name="repositories")
    op.drop_index("ix_repositories_stargazer_count", table_name="repositories")
    op.drop_index("ix_repositories_language", table_name="repositories")
    op.drop_table("repositories")
