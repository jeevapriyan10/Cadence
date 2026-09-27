"""create_echo_tables

Revision ID: e9b87a65c432
Revises: c5ae6d5f5564
Create Date: 2026-09-27 11:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e9b87a65c432'
down_revision: Union[str, None] = 'c5ae6d5f5564'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'network_runs',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('profile_name', sa.String(), nullable=False),
        sa.Column('seed', sa.Integer(), nullable=False),
        sa.Column('section_count', sa.Integer(), nullable=False),
        sa.Column('train_count', sa.Integer(), nullable=False),
        sa.Column('task_count', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'decision_records',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('network_run_id', sa.String(), nullable=False),
        sa.Column('strategy', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('objective_value', sa.Float(), nullable=True),
        sa.Column('wall_time_seconds', sa.Float(), nullable=False),
        sa.Column('rl_fallback_triggered', sa.Boolean(), nullable=False),
        sa.Column('scheduled_blocks_snapshot', sa.JSON(), nullable=False),
        sa.Column('is_replan', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['network_run_id'], ['network_runs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('decision_records', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_decision_records_network_run_id'), ['network_run_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('decision_records', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_decision_records_network_run_id'))

    op.drop_table('decision_records')
    op.drop_table('network_runs')
