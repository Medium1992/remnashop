"""Panel users the bot switched off after a long lapse (docs/s/README.md)."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "s0004"
down_revision: Union[str, None] = "s0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "s_rule_disabled",
        sa.Column("user_remna_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "disabled_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_s_rule_disabled_user_id", "s_rule_disabled", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_s_rule_disabled_user_id", table_name="s_rule_disabled")
    op.drop_table("s_rule_disabled")
