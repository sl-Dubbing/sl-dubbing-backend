"""sync models: add avatar_key, job_id, credits_used, unify table names

Revision ID: 20260501_sync_models
Revises: <previous_revision_id>
Create Date: 2026-05-01 22:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '20260501_sync_models'
down_revision = '<previous_revision_id>'
branch_labels = None
depends_on = None


def upgrade():
    # users: add avatar_key and avatar if missing
    with op.batch_alter_table('users') as batch_op:
        try:
            batch_op.add_column(sa.Column('avatar_key', sa.String(length=255), nullable=True))
        except Exception:
            pass
        try:
            batch_op.add_column(sa.Column('avatar', sa.String(length=1000), nullable=True))
        except Exception:
            pass
        try:
            batch_op.create_index('ix_users_supabase_id', ['supabase_id'])
        except Exception:
            pass

    # dubbing_jobs: add credits_used and voice_mode if missing
    with op.batch_alter_table('dubbing_jobs') as batch_op:
        try:
            batch_op.add_column(sa.Column('credits_used', sa.Integer(), nullable=False, server_default='0'))
        except Exception:
            pass
        try:
            batch_op.add_column(sa.Column('voice_mode', sa.String(length=50), nullable=True, server_default='source'))
        except Exception:
            pass
        try:
            batch_op.create_index('ix_dubbing_jobs_created_at', ['created_at'])
        except Exception:
            pass

    # credit_transactions: add job_id and index
    with op.batch_alter_table('credit_transactions') as batch_op:
        try:
            batch_op.add_column(sa.Column('job_id', sa.String(length=36), nullable=True))
        except Exception:
            pass
        try:
            batch_op.create_index('ix_credit_transactions_job_id', ['job_id'])
        except Exception:
            pass

    # create FK credit_transactions.job_id -> dubbing_jobs.id
    try:
        op.create_foreign_key(
            'fk_credit_transactions_job',
            'credit_transactions',
            'dubbing_jobs',
            ['job_id'],
            ['id'],
            ondelete='SET NULL'
        )
    except Exception:
        pass


def downgrade():
    # drop FK and job_id
    try:
        op.drop_constraint('fk_credit_transactions_job', 'credit_transactions', type_='foreignkey')
    except Exception:
        pass
    with op.batch_alter_table('credit_transactions') as batch_op:
        try:
            batch_op.drop_index('ix_credit_transactions_job_id')
        except Exception:
            pass
        try:
            batch_op.drop_column('job_id')
        except Exception:
            pass

    # drop credits_used and voice_mode from dubbing_jobs
    with op.batch_alter_table('dubbing_jobs') as batch_op:
        try:
            batch_op.drop_column('credits_used')
        except Exception:
            pass
        try:
            batch_op.drop_column('voice_mode')
        except Exception:
            pass
        try:
            batch_op.drop_index('ix_dubbing_jobs_created_at')
        except Exception:
            pass

    # drop avatar_key and avatar from users
    with op.batch_alter_table('users') as batch_op:
        try:
            batch_op.drop_column('avatar_key')
        except Exception:
            pass
        try:
            batch_op.drop_column('avatar')
        except Exception:
            pass
        try:
            batch_op.drop_index('ix_users_supabase_id')
        except Exception:
            pass
