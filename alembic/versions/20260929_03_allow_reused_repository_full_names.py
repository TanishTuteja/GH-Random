"""Allow mutable GitHub full names to be reused by different repository IDs."""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260929_03"
down_revision: str | None = "20260929_02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Remove uniqueness from GitHub's mutable owner/name representation."""

    op.drop_constraint("uq_repositories_full_name", "repositories", type_="unique")


def downgrade() -> None:
    """Restore the former uniqueness constraint."""

    op.create_unique_constraint("uq_repositories_full_name", "repositories", ["full_name"])
