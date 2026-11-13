"""widen progres_id and auth_txn_ref for encrypted values

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('sync_record') as b:
        b.alter_column('progres_id', existing_type=sa.String(64), type_=sa.String(512), existing_nullable=False)
        b.alter_column('auth_txn_ref', existing_type=sa.String(128), type_=sa.String(512), existing_nullable=True)


def downgrade() -> None:
    # Fails on PostgreSQL if encrypted values longer than the old limits are stored: decrypt/scrub first.
    with op.batch_alter_table('sync_record') as b:
        b.alter_column('progres_id', existing_type=sa.String(512), type_=sa.String(64), existing_nullable=False)
        b.alter_column('auth_txn_ref', existing_type=sa.String(512), type_=sa.String(128), existing_nullable=True)
