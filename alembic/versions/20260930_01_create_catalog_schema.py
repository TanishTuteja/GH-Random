"""Create the catalog schema baseline."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260930_01"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the complete pre-production repository catalog schema."""

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
        sa.Column("github_created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("github_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("github_pushed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "open_issues_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("topics", sa.JSON(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "stargazer_count >= 0",
            name="ck_repositories_stargazer_count_nonnegative",
        ),
        sa.CheckConstraint("fork_count >= 0", name="ck_repositories_fork_count_nonnegative"),
        sa.CheckConstraint(
            "open_issues_count >= 0",
            name="ck_repositories_open_issues_count_nonnegative",
        ),
        sa.PrimaryKeyConstraint("github_id"),
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
    op.create_index(
        "ix_repositories_github_pushed_at",
        "repositories",
        ["github_pushed_at"],
        unique=False,
    )

    op.create_table(
        "catalog_generations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('staging', 'active', 'retired', 'failed')",
            name="ck_catalog_generations_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_catalog_generations_status",
        "catalog_generations",
        ["status"],
        unique=False,
    )
    op.create_index(
        "uq_catalog_generations_one_active",
        "catalog_generations",
        ["status"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "crawl_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("generation_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("sort_key", sa.String(length=50), nullable=False),
        sa.Column("sort_order", sa.String(length=4), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requests_made", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("repositories_seen", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("plan_reference_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("plan_fingerprint", sa.String(length=64), nullable=True),
        sa.Column(
            "next_work_item_index",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("next_page", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("current_work_item_page_count", sa.Integer(), nullable=True),
        sa.Column("resume_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'failed')",
            name="ck_crawl_runs_status",
        ),
        sa.ForeignKeyConstraint(["generation_id"], ["catalog_generations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("generation_id"),
    )
    op.create_index("ix_crawl_runs_status", "crawl_runs", ["status"], unique=False)

    op.create_table(
        "generation_repositories",
        sa.Column("generation_id", sa.Integer(), nullable=False),
        sa.Column("github_id", sa.BigInteger(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["generation_id"],
            ["catalog_generations.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["github_id"], ["repositories.github_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("generation_id", "github_id"),
    )
    op.create_index(
        "ix_generation_repositories_last_seen_at",
        "generation_repositories",
        ["last_seen_at"],
        unique=False,
    )


def downgrade() -> None:
    """Remove the complete catalog schema."""

    op.drop_index("ix_generation_repositories_last_seen_at", table_name="generation_repositories")
    op.drop_table("generation_repositories")
    op.drop_index("ix_crawl_runs_status", table_name="crawl_runs")
    op.drop_table("crawl_runs")
    op.drop_index("uq_catalog_generations_one_active", table_name="catalog_generations")
    op.drop_index("ix_catalog_generations_status", table_name="catalog_generations")
    op.drop_table("catalog_generations")
    op.drop_index("ix_repositories_github_pushed_at", table_name="repositories")
    op.drop_index("ix_repositories_github_updated_at", table_name="repositories")
    op.drop_index("ix_repositories_stargazer_count", table_name="repositories")
    op.drop_index("ix_repositories_language", table_name="repositories")
    op.drop_table("repositories")
