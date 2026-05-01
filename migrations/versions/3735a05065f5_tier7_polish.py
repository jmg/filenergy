"""tier7: file soft-delete, conversation pin/archive, collection share links

Revision ID: 3735a05065f5
Revises: cccbc12f8d17
Create Date: 2026-05-01 04:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "3735a05065f5"
down_revision = "cccbc12f8d17"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("file", sa.Column("deleted_at", sa.DateTime(), nullable=True))
    op.create_index(
        op.f("ix_file_deleted_at"), "file", ["deleted_at"], unique=False,
    )
    op.add_column("conversation", sa.Column("pinned_at", sa.DateTime(), nullable=True))
    op.add_column("conversation", sa.Column("archived_at", sa.DateTime(), nullable=True))

    op.create_table(
        "collection_share_link",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("collection_id", sa.Integer(), nullable=True),
        sa.Column("token", sa.String(length=64), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("view_count", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["collection_id"], ["collection.id"]),
        sa.ForeignKeyConstraint(["created_by_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_collection_share_link_collection_id"),
        "collection_share_link", ["collection_id"], unique=False,
    )
    op.create_index(
        op.f("ix_collection_share_link_token"),
        "collection_share_link", ["token"], unique=True,
    )


def downgrade():
    op.drop_index(op.f("ix_collection_share_link_token"), table_name="collection_share_link")
    op.drop_index(op.f("ix_collection_share_link_collection_id"), table_name="collection_share_link")
    op.drop_table("collection_share_link")
    op.drop_column("conversation", "archived_at")
    op.drop_column("conversation", "pinned_at")
    op.drop_index(op.f("ix_file_deleted_at"), table_name="file")
    op.drop_column("file", "deleted_at")
