from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "s0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "s_user_blocks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("telegram_id", sa.BigInteger(), nullable=True),
        sa.Column("reason", sa.String(512), nullable=False, server_default=""),
        sa.Column("until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_s_user_blocks_user_id", "s_user_blocks", ["user_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_s_user_blocks_user_id", table_name="s_user_blocks")
    op.drop_table("s_user_blocks")
