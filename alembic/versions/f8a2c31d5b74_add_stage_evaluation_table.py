"""add stage_evaluation table

Revision ID: f8a2c31d5b74
Revises: c1f29a4b9156
Create Date: 2026-09-06

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f8a2c31d5b74'
down_revision = 'c1f29a4b9156'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'stage_evaluation',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('task_id', sa.String(length=64), nullable=False),
        sa.Column('run_id', sa.String(length=64), nullable=False),
        sa.Column('stage_index', sa.Integer(), nullable=False),
        sa.Column('attempt', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('defense_round', sa.Integer(), nullable=False, server_default='0'),
        sa.Column(
            'status',
            sa.Enum('PASSED', 'RETRY', 'DEFEND', 'DEGRADED_PASS', name='stage_eval_status'),
            nullable=False,
        ),
        sa.Column('rule_result', sa.JSON(), nullable=True),
        sa.Column('forward_result', sa.JSON(), nullable=True),
        sa.Column('reverse_result', sa.JSON(), nullable=True),
        sa.Column('criteria_scores', sa.JSON(), nullable=True),
        sa.Column('weighted_score', sa.Float(), nullable=True),
        sa.Column('feedback', sa.JSON(), nullable=True),
        sa.Column('acceptance_snapshot', sa.JSON(), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False
        ),
        sa.PrimaryKeyConstraint('id'),
        mysql_engine='InnoDB',
        mysql_charset='utf8mb4',
    )
    op.create_index('ix_stage_evaluation_task_id', 'stage_evaluation', ['task_id'])
    op.create_index('ix_stage_evaluation_stage', 'stage_evaluation', ['task_id', 'stage_index'])


def downgrade() -> None:
    op.drop_index('ix_stage_evaluation_stage', table_name='stage_evaluation')
    op.drop_index('ix_stage_evaluation_task_id', table_name='stage_evaluation')
    op.drop_table('stage_evaluation')
