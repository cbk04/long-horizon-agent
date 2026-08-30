"""add task_event table

Revision ID: bbc2a7fc169a
Revises: 3e439e7f00e3
Create Date: 2026-08-23 17:30:31.585386

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'bbc2a7fc169a'
down_revision = '3e439e7f00e3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'task_event',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('task_id', sa.String(length=64), nullable=False),
        sa.Column('stream_id', sa.String(length=64), nullable=False),
        sa.Column('type', sa.String(length=64), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        mysql_engine='InnoDB',
        mysql_charset='utf8mb4',
    )
    op.create_index('ix_task_event_task_id', 'task_event', ['task_id'])
    op.create_index('ix_task_event_type', 'task_event', ['type'])


def downgrade() -> None:
    op.drop_index('ix_task_event_type', table_name='task_event')
    op.drop_index('ix_task_event_task_id', table_name='task_event')
    op.drop_table('task_event')
