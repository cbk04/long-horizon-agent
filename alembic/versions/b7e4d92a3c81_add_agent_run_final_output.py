"""add agent_run.final_output

Revision ID: b7e4d92a3c81
Revises: f8a2c31d5b74
Create Date: 2026-09-06

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b7e4d92a3c81'
down_revision = 'f8a2c31d5b74'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('agent_run', sa.Column('final_output', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('agent_run', 'final_output')
