"""Receipts of the self-employed in "Мой налог" (docs/s/README.md)."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "s0003"
down_revision: Union[str, None] = "s0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _now() -> sa.Column:
    return sa.Column(
        "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


def upgrade() -> None:
    op.create_table(
        "s_nalog_account",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("inn", sa.String(12), nullable=True),
        sa.Column("method", sa.String(16), nullable=True),
        sa.Column("phone", sa.Text(), nullable=True),
        sa.Column("password", sa.Text(), nullable=True),
        sa.Column("refresh_token", sa.Text(), nullable=True),
        sa.Column("token", sa.Text(), nullable=True),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("device_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("proxy", sa.Text(), nullable=True),
        sa.Column("lost_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lost_reason", sa.Text(), nullable=True),
        _now(),
    )
    op.create_table(
        "s_receipts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("payment_id", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        # The receipt outlives the account: the income stays in the tax books.
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("purchase_type", sa.String(16), nullable=False),
        sa.Column("duration", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, index=True),
        sa.Column("name", sa.String(256), nullable=False, server_default=""),
        sa.Column("receipt_uuid", sa.String(64), nullable=True),
        sa.Column("inn", sa.String(12), nullable=True),
        # Set when the receipt was made out to an organisation instead.
        sa.Column("client_inn", sa.String(12), nullable=True),
        sa.Column("client_name", sa.String(256), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_try_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("delivery_error", sa.Text(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        _now(),
    )


def downgrade() -> None:
    op.drop_table("s_receipts")
    op.drop_table("s_nalog_account")
