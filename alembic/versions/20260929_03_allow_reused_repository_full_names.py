"""Allow mutable GitHub full names to be reused by different repository IDs."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260929_03"
down_revision: str | None = "20260929_02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQLITE_UNIQUE_CONVENTION = {
    "uq": "uq_%(table_name)s_%(column_0_name)s",
}


def upgrade() -> None:
    """Remove uniqueness from GitHub's mutable owner/name representation."""

    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table(
            "repositories",
            naming_convention=SQLITE_UNIQUE_CONVENTION,
        ) as batch_op:
            batch_op.drop_constraint("uq_repositories_full_name", type_="unique")
        return

    constraint_name = _full_name_unique_constraint_name(bind)
    op.drop_constraint(constraint_name, "repositories", type_="unique")


def downgrade() -> None:
    """Restore the former uniqueness constraint."""

    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table(
            "repositories",
            naming_convention=SQLITE_UNIQUE_CONVENTION,
        ) as batch_op:
            batch_op.create_unique_constraint("uq_repositories_full_name", ["full_name"])
        return

    op.create_unique_constraint("uq_repositories_full_name", "repositories", ["full_name"])


def _full_name_unique_constraint_name(bind: sa.Connection) -> str:
    unique_constraints = sa.inspect(bind).get_unique_constraints("repositories")
    for constraint in unique_constraints:
        if constraint.get("column_names") == ["full_name"] and constraint.get("name"):
            return str(constraint["name"])
    raise RuntimeError("The repositories full_name uniqueness constraint was not found.")
