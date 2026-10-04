"""phase 2 telephony: provider call sid on call sessions

Real phone webhooks carry a provider CallSid; matching sessions by caller
phone tail alone is ambiguous when the same number calls twice or provider
retries interleave. The CallSid becomes the exact-match lookup key (phone
tail stays as the first-webhook fallback).

Revision ID: c4e8a1f7d290
Revises: b7d2f4a91c03
Create Date: 2026-10-04

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c4e8a1f7d290'
down_revision: Union[str, None] = 'b7d2f4a91c03'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('call_sessions',
                  sa.Column('provider_call_sid', sa.String(length=64), nullable=True))
    op.create_index(op.f('ix_call_sessions_provider_call_sid'),
                    'call_sessions', ['provider_call_sid'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_call_sessions_provider_call_sid'),
                  table_name='call_sessions')
    op.drop_column('call_sessions', 'provider_call_sid')
