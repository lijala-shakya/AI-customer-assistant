"""add pending ticket email verifications

Revision ID: 7c2a8be160b1
Revises: 2a1c9f0e45d7
"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "7c2a8be160b1"
down_revision: Union[str, None] = "2a1c9f0e45d7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ticket_verification",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("thread_id", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )


def downgrade() -> None:
    op.drop_table("ticket_verification")
