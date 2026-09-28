"""Add crawl generations and GitHub pushed-at metadata."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260929_02"
down_revision: str | None = "20260929_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add canonical crawl metadata plus active/staging generation tables."""

    with op.batch_alter_table("repositories") as batch_op:
        batch_op.alter_column(
            "created_at",
            new_column_name="github_created_at",
            existing_type=sa.DateTime(timezone=True),
        )
        batch_op.alter_column(
            "last_crawled_at",
            new_column_name="last_seen_at",
            existing_type=sa.DateTime(timezone=True),
        )
        batch_op.add_column(
            sa.Column("github_pushed_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "open_issues_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.add_column(sa.Column("topics", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_check_constraint(
            "ck_repositories_open_issues_count_nonnegative",
            "open_issues_count >= 0",
        )

    op.execute("UPDATE repositories SET first_seen_at = last_seen_at")

    with op.batch_alter_table("repositories") as batch_op:
        batch_op.alter_column(
            "first_seen_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
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
        sqlite_where=sa.text("status = 'active'"),
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
    """Remove generation tables and the additional repository metadata."""

    op.drop_index("ix_generation_repositories_last_seen_at", table_name="generation_repositories")
    op.drop_table("generation_repositories")
    op.drop_index("ix_crawl_runs_status", table_name="crawl_runs")
    op.drop_table("crawl_runs")
    op.drop_index("uq_catalog_generations_one_active", table_name="catalog_generations")
    op.drop_index("ix_catalog_generations_status", table_name="catalog_generations")
    op.drop_table("catalog_generations")
    op.drop_index("ix_repositories_github_pushed_at", table_name="repositories")

    with op.batch_alter_table("repositories") as batch_op:
        batch_op.drop_constraint(
            "ck_repositories_open_issues_count_nonnegative",
            type_="check",
        )
        batch_op.drop_column("first_seen_at")
        batch_op.drop_column("topics")
        batch_op.drop_column("open_issues_count")
        batch_op.drop_column("github_pushed_at")
        batch_op.alter_column(
            "last_seen_at",
            new_column_name="last_crawled_at",
            existing_type=sa.DateTime(timezone=True),
        )
        batch_op.alter_column(
            "github_created_at",
            new_column_name="created_at",
            existing_type=sa.DateTime(timezone=True),
        )
