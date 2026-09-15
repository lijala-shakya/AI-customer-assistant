"""Add security columns for email verification and job persistence.

Revision ID: add_security_columns_20260902
Revises: 7c2a8be160b1
Create Date: 2026-09-02

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'add_security_columns_20260902'
down_revision = '7c2a8be160b1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add security-related columns for email verification and job persistence."""
    
    # Add columns to ticket_verification table
    op.add_column(
        'ticket_verification',
        sa.Column('resend_count', sa.Integer(), nullable=False, server_default='0')
    )
    op.add_column(
        'ticket_verification',
        sa.Column('send_attempts', sa.Integer(), nullable=False, server_default='1')
    )
    op.add_column(
        'ticket_verification',
        sa.Column(
            'last_sent_at',
            sa.DateTime(timezone=True),
            nullable=True
        )
    )
    op.add_column(
        'ticket_verification',
        sa.Column(
            'verified_at',
            sa.DateTime(timezone=True),
            nullable=True
        )
    )
    
    # Add columns to knowledge_injection_job table
    op.add_column(
        'knowledge_injection_job',
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0')
    )


def downgrade() -> None:
    """Revert security columns."""
    
    # Remove from knowledge_injection_job
    op.drop_column('knowledge_injection_job', 'attempts')
    
    # Remove from ticket_verification
    op.drop_column('ticket_verification', 'verified_at')
    op.drop_column('ticket_verification', 'last_sent_at')
    op.drop_column('ticket_verification', 'send_attempts')
    op.drop_column('ticket_verification', 'resend_count')
