"""phase 3: fcm device tokens for staff mobile apps

Push is the delivery mechanism for "the moment a call ends it shows up on
the ASHA's phone": every dispatch state change fans out to the FCM tokens
registered by logged-in staff devices.

Revision ID: d5f9b2e8c341
Revises: c4e8a1f7d290
Create Date: 2026-10-04

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import app.db  # noqa: F401  (UtcDateTime columns render as app.db.UtcDateTime)


revision: str = 'd5f9b2e8c341'
down_revision: Union[str, None] = 'c4e8a1f7d290'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'device_tokens',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('token', sa.String(length=256), nullable=False),
        sa.Column('platform', sa.String(length=16), nullable=False),
        sa.Column('label', sa.String(length=120), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_at', app.db.UtcDateTime(timezone=True), nullable=False),
        sa.Column('last_seen_at', app.db.UtcDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_device_tokens_user_id'), 'device_tokens',
                    ['user_id'], unique=False)
    op.create_index(op.f('ix_device_tokens_token'), 'device_tokens',
                    ['token'], unique=True)


def downgrade() -> None:
    op.drop_index(op.f('ix_device_tokens_token'), table_name='device_tokens')
    op.drop_index(op.f('ix_device_tokens_user_id'), table_name='device_tokens')
    op.drop_table('device_tokens')
