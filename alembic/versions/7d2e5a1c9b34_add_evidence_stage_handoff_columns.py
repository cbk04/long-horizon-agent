"""add evidence stage-handoff columns

Revision ID: 7d2e5a1c9b34
Revises: 9c4b7e21a6d3
Create Date: 2026-09-08

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = '7d2e5a1c9b34'
down_revision = '9c4b7e21a6d3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('evidence', sa.Column('run_id', sa.String(length=64), nullable=True))
    op.add_column('evidence', sa.Column('stage_index', sa.Integer(), nullable=True))
    op.add_column('evidence', sa.Column('summary', sa.Text(), nullable=True))
    op.add_column('evidence', sa.Column('content', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('evidence', 'content')
    op.drop_column('evidence', 'summary')
    op.drop_column('evidence', 'stage_index')
    op.drop_column('evidence', 'run_id')
