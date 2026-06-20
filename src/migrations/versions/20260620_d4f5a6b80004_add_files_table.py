"""add files table (реестр блобов файлового хранилища)

Revision ID: d4f5a6b80004
Revises: c3e4f5a70003
Create Date: 2026-06-20 17:00:00.000000+00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'd4f5a6b80004'
down_revision: str | None = 'c3e4f5a70003'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'files',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('storage_key', sa.Text(), nullable=False),
        sa.Column('bucket', sa.String(length=63), nullable=False),
        sa.Column('category', sa.String(length=32), nullable=False),
        sa.Column('original_filename', sa.String(length=255), nullable=False),
        sa.Column('content_type', sa.String(length=127), nullable=False),
        sa.Column('size_bytes', sa.BigInteger(), nullable=False),
        sa.Column('checksum_sha256', sa.String(length=64), nullable=True),
        sa.Column(
            'is_attached',
            sa.Boolean(),
            server_default=sa.text('false'),
            nullable=False,
        ),
        sa.Column('organization_id', sa.UUID(), nullable=True),
        sa.Column('owner_user_id', sa.UUID(), nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ['organization_id'], ['organizations.id'], ondelete='CASCADE'
        ),
        sa.ForeignKeyConstraint(['owner_user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_files_storage_key', 'files', ['storage_key'], unique=True)
    op.create_index('ix_files_category', 'files', ['category'])
    op.create_index('ix_files_organization_id', 'files', ['organization_id'])
    op.create_index('ix_files_owner_user_id', 'files', ['owner_user_id'])
    op.create_index('ix_files_attached_created', 'files', ['is_attached', 'created_at'])


def downgrade() -> None:
    op.drop_index('ix_files_attached_created', table_name='files')
    op.drop_index('ix_files_owner_user_id', table_name='files')
    op.drop_index('ix_files_organization_id', table_name='files')
    op.drop_index('ix_files_category', table_name='files')
    op.drop_index('ix_files_storage_key', table_name='files')
    op.drop_table('files')
