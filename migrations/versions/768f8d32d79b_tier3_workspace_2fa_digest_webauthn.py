"""tier3: workspace 2FA enforcement, digest opt-out, WebAuthn

Revision ID: 768f8d32d79b
Revises: bde95b2679fe
Create Date: 2026-04-30 22:15:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "768f8d32d79b"
down_revision = "bde95b2679fe"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "workspace",
        sa.Column("require_2fa", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "user",
        sa.Column("weekly_digest", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "user",
        sa.Column("last_digest_sent_at", sa.DateTime(), nullable=True),
    )

    op.create_table(
        "webauthn_credential",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("credential_id", sa.String(length=512), nullable=True),
        sa.Column("public_key", sa.Text(), nullable=True),
        sa.Column("sign_count", sa.Integer(), nullable=True),
        sa.Column("label", sa.String(length=120), nullable=True),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_webauthn_credential_user_id"),
        "webauthn_credential", ["user_id"], unique=False,
    )
    op.create_index(
        op.f("ix_webauthn_credential_credential_id"),
        "webauthn_credential", ["credential_id"], unique=True,
    )


def downgrade():
    op.drop_index(
        op.f("ix_webauthn_credential_credential_id"),
        table_name="webauthn_credential",
    )
    op.drop_index(
        op.f("ix_webauthn_credential_user_id"),
        table_name="webauthn_credential",
    )
    op.drop_table("webauthn_credential")
    op.drop_column("user", "last_digest_sent_at")
    op.drop_column("user", "weekly_digest")
    op.drop_column("workspace", "require_2fa")
