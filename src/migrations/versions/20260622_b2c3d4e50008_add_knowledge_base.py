"""база знаний: knowledge_nodes + knowledge_node_access + knowledge_node_files

Revision ID: b2c3d4e50008
Revises: a1b2c3d40007
Create Date: 2026-06-22 12:00:00.000000+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b2c3d4e50008"
down_revision: str | None = "a1b2c3d40007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "knowledge_nodes",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("icon", sa.String(length=64), nullable=True),
        sa.Column("position", sa.Integer(), server_default="0", nullable=False),
        sa.Column("all_members", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("schema_version", sa.SmallInteger(), server_default="1", nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["parent_id"], ["knowledge_nodes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_knowledge_nodes_organization_id",
        "knowledge_nodes",
        ["organization_id"],
    )
    op.create_index("ix_knowledge_nodes_parent_id", "knowledge_nodes", ["parent_id"])
    op.create_index(
        "ix_knowledge_nodes_org_parent_position",
        "knowledge_nodes",
        ["organization_id", "parent_id", "position"],
    )

    op.create_table(
        "knowledge_node_access",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("node_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subject_type", sa.String(length=16), nullable=False),
        sa.Column("role_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("member_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("effect", sa.String(length=8), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(subject_type = 'role' AND role_id IS NOT NULL AND member_user_id IS NULL) "
            "OR (subject_type = 'member' AND member_user_id IS NOT NULL AND role_id IS NULL)",
            name="ck_knowledge_access_subject",
        ),
        sa.ForeignKeyConstraint(["node_id"], ["knowledge_nodes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["role_id"], ["organization_roles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["member_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_knowledge_node_access_node_id",
        "knowledge_node_access",
        ["node_id"],
    )
    op.create_index(
        "uq_knowledge_access_role",
        "knowledge_node_access",
        ["node_id", "role_id"],
        unique=True,
        postgresql_where=sa.text("subject_type = 'role'"),
    )
    op.create_index(
        "uq_knowledge_access_member",
        "knowledge_node_access",
        ["node_id", "member_user_id"],
        unique=True,
        postgresql_where=sa.text("subject_type = 'member'"),
    )

    op.create_table(
        "knowledge_node_files",
        sa.Column("node_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("file_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["node_id"], ["knowledge_nodes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("node_id", "file_id"),
        sa.UniqueConstraint("file_id", name="uq_knowledge_node_files_file_id"),
    )
    op.create_index(
        "ix_knowledge_node_files_node_id",
        "knowledge_node_files",
        ["node_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_knowledge_node_files_node_id", table_name="knowledge_node_files")
    op.drop_table("knowledge_node_files")

    op.drop_index("uq_knowledge_access_member", table_name="knowledge_node_access")
    op.drop_index("uq_knowledge_access_role", table_name="knowledge_node_access")
    op.drop_index("ix_knowledge_node_access_node_id", table_name="knowledge_node_access")
    op.drop_table("knowledge_node_access")

    op.drop_index("ix_knowledge_nodes_org_parent_position", table_name="knowledge_nodes")
    op.drop_index("ix_knowledge_nodes_parent_id", table_name="knowledge_nodes")
    op.drop_index("ix_knowledge_nodes_organization_id", table_name="knowledge_nodes")
    op.drop_table("knowledge_nodes")
