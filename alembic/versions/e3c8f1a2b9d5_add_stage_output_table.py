"""add stage_output table

Revision ID: e3c8f1a2b9d5
Revises: 7d2e5a1c9b34
Create Date: 2026-09-08

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = 'e3c8f1a2b9d5'
down_revision = '7d2e5a1c9b34'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'stage_output',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('task_id', sa.String(length=64), nullable=False),
        sa.Column('run_id', sa.String(length=64), nullable=True),
        sa.Column('stage_index', sa.Integer(), nullable=False),
        sa.Column('objective', sa.Text(), nullable=False),
        sa.Column('conclusion', sa.Text(), nullable=False),
        sa.Column('key_findings', sa.JSON(), nullable=True),
        sa.Column('evidence_index', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_stage_output_task_id', 'stage_output', ['task_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_stage_output_task_id', table_name='stage_output')
    op.drop_table('stage_output')
