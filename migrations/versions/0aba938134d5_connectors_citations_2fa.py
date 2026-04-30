"""connectors, message citations, encrypted totp_secret

Revision ID: 0aba938134d5
Revises: 886d0d241620
Create Date: 2026-04-30 20:01:15.784676

Adds tables and column-type changes that landed after the initial
schema:

  * `connector_account` — Notion/Slack/Dropbox/Drive OAuth state, plus
    the per-account `sync_cursor` for incremental sync.
  * `message_citation` — chunk-level provenance index pointing each
    assistant Message at the Chunks it cited.
  * `user.totp_secret` — was `VARCHAR(64)`; widened to `Text` so the
    `enc:` prefix from `EncryptedText` fits.

`access_token` / `refresh_token` / `embedding` / `text_content` are
declared `EncryptedText` in the model but stored as plain `Text` on
disk (the `enc:` prefix is the only on-disk difference). The
migration uses `sa.Text()` to keep the schema portable across
Postgres / SQLite.
"""
from alembic import op
import sqlalchemy as sa


revision = '0aba938134d5'
down_revision = '886d0d241620'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'connector_account',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('workspace_id', sa.Integer(), nullable=True),
        sa.Column('kind', sa.String(length=32), nullable=True),
        sa.Column('account_label', sa.String(length=255), nullable=True),
        sa.Column('access_token', sa.Text(), nullable=True),
        sa.Column('refresh_token', sa.Text(), nullable=True),
        sa.Column('sync_cursor', sa.Text(), nullable=True),
        sa.Column('expires_at', sa.DateTime(), nullable=True),
        sa.Column('last_synced_at', sa.DateTime(), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['workspace_id'], ['workspace.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_connector_account_kind'),
        'connector_account', ['kind'], unique=False,
    )
    op.create_index(
        op.f('ix_connector_account_workspace_id'),
        'connector_account', ['workspace_id'], unique=False,
    )

    op.create_table(
        'message_citation',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('message_id', sa.Integer(), nullable=True),
        sa.Column('chunk_id', sa.Integer(), nullable=True),
        sa.Column('score', sa.Float(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['chunk_id'], ['chunk.id']),
        sa.ForeignKeyConstraint(['message_id'], ['message.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_message_citation_chunk_id'),
        'message_citation', ['chunk_id'], unique=False,
    )
    op.create_index(
        op.f('ix_message_citation_message_id'),
        'message_citation', ['message_id'], unique=False,
    )

    with op.batch_alter_table('user') as batch:
        batch.alter_column(
            'totp_secret',
            existing_type=sa.VARCHAR(length=64),
            type_=sa.Text(),
            existing_nullable=True,
        )


def downgrade():
    with op.batch_alter_table('user') as batch:
        batch.alter_column(
            'totp_secret',
            existing_type=sa.Text(),
            type_=sa.VARCHAR(length=64),
            existing_nullable=True,
        )
    op.drop_index(op.f('ix_message_citation_message_id'), table_name='message_citation')
    op.drop_index(op.f('ix_message_citation_chunk_id'), table_name='message_citation')
    op.drop_table('message_citation')
    op.drop_index(op.f('ix_connector_account_workspace_id'), table_name='connector_account')
    op.drop_index(op.f('ix_connector_account_kind'), table_name='connector_account')
    op.drop_table('connector_account')
