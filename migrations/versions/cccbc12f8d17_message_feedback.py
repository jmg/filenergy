"""tier5: message feedback (thumbs up/down)

Revision ID: cccbc12f8d17
Revises: 768f8d32d79b
Create Date: 2026-05-01 00:30:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "cccbc12f8d17"
down_revision = "768f8d32d79b"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "message_feedback",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("rating", sa.String(length=8), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["message_id"], ["message.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("message_id", "user_id", name="uq_message_feedback"),
    )
    op.create_index(
        op.f("ix_message_feedback_message_id"),
        "message_feedback", ["message_id"], unique=False,
    )
    op.create_index(
        op.f("ix_message_feedback_user_id"),
        "message_feedback", ["user_id"], unique=False,
    )


def downgrade():
    op.drop_index(op.f("ix_message_feedback_user_id"), table_name="message_feedback")
    op.drop_index(op.f("ix_message_feedback_message_id"), table_name="message_feedback")
    op.drop_table("message_feedback")
