"""Add durable page-level checkpoints for resumable catalog crawls."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260929_04"
down_revision: str | None = "20260929_03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Persist the next GitHub Search page after each successful ingestion."""

    op.add_column(
        "crawl_runs",
        sa.Column("plan_reference_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "crawl_runs",
        sa.Column("plan_fingerprint", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "crawl_runs",
        sa.Column(
            "next_work_item_index",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "crawl_runs",
        sa.Column("next_page", sa.Integer(), nullable=False, server_default=sa.text("1")),
    )
    op.add_column(
        "crawl_runs",
        sa.Column("current_work_item_page_count", sa.Integer(), nullable=True),
    )
    op.add_column(
        "crawl_runs",
        sa.Column("resume_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )

    # Historical runs predate durable checkpoints. Their absent fingerprint keeps
    # them distinguishable from exact, post-migration resume candidates.
    op.execute("UPDATE crawl_runs SET plan_reference_at = started_at")


def downgrade() -> None:
    """Remove durable crawl-resume state."""

    op.drop_column("crawl_runs", "resume_count")
    op.drop_column("crawl_runs", "current_work_item_page_count")
    op.drop_column("crawl_runs", "next_page")
    op.drop_column("crawl_runs", "next_work_item_index")
    op.drop_column("crawl_runs", "plan_fingerprint")
    op.drop_column("crawl_runs", "plan_reference_at")
