"""What of a subscription an admin set by hand stays on renewal (docs/s/README.md)."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "s0002"
down_revision: Union[str, None] = "s0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "s_kept_limits",
        sa.Column(
            "subscription_id",
            sa.Integer(),
            sa.ForeignKey("subscriptions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("devices", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("traffic", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("squads", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("s_kept_limits")
