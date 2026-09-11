"""add agent_message table

Revision ID: 9c4b7e21a6d3
Revises: b7e4d92a3c81
Create Date: 2026-09-06

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '9c4b7e21a6d3'
down_revision = 'b7e4d92a3c81'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'agent_message',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('task_id', sa.String(length=64), nullable=False),
        sa.Column('run_id', sa.String(length=64), nullable=True),
        sa.Column('thread_id', sa.String(length=128), nullable=False),
        sa.Column('seq', sa.Integer(), nullable=False),
        sa.Column('message_id', sa.String(length=64), nullable=False),
        sa.Column('role', sa.String(length=16), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('reasoning_content', sa.Text(), nullable=True),
        sa.Column('tool_name', sa.String(length=64), nullable=True),
        sa.Column('tool_call_id', sa.String(length=64), nullable=True),
        sa.Column('tool_calls', sa.JSON(), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('message_id'),
        mysql_engine='InnoDB',
        mysql_charset='utf8mb4',
    )
    op.create_index('ix_agent_message_task_id', 'agent_message', ['task_id'])
    op.create_index('ix_agent_message_thread_seq', 'agent_message', ['thread_id', 'seq'])


def downgrade() -> None:
    op.drop_index('ix_agent_message_thread_seq', table_name='agent_message')
    op.drop_index('ix_agent_message_task_id', table_name='agent_message')
    op.drop_table('agent_message')
