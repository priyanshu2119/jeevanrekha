"""phase 1 hardening: region phone numbers, system status, perf indexes

Adds:
* region_phone_numbers -- maps provider DIDs (the number a caller dials) to a
  region so real inbound phone calls resolve their routing configuration
  (without this, Track B backup chains never fire for phone callers).
* system_status -- small key/value table; the scheduler writes a heartbeat row
  so /readyz and monitoring can prove the escalation worker is alive.
* Composite indexes for the two hottest queries: the scheduler's timeout scan
  (every tick) and region-scoped call log listings.

Revision ID: b7d2f4a91c03
Revises: e951365fb128
Create Date: 2026-10-04

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import app.db  # noqa: F401  (UtcDateTime columns render as app.db.UtcDateTime)


revision: str = 'b7d2f4a91c03'
down_revision: Union[str, None] = 'e951365fb128'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'region_phone_numbers',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('region_id', sa.Integer(), nullable=False),
        sa.Column('provider', sa.String(length=16), nullable=False),
        sa.Column('phone_number', sa.String(length=32), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['region_id'], ['regions.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('provider', 'phone_number',
                            name='uq_region_phone_provider_number'),
    )
    op.create_index(op.f('ix_region_phone_numbers_region_id'),
                    'region_phone_numbers', ['region_id'], unique=False)
    op.create_index(op.f('ix_region_phone_numbers_phone_number'),
                    'region_phone_numbers', ['phone_number'], unique=False)

    op.create_table(
        'system_status',
        sa.Column('key', sa.String(length=48), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('updated_at', app.db.UtcDateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('key'),
    )

    # Hot path: scheduler scans (action, resolved, due_at) every tick.
    op.create_index('ix_dispatch_events_timeout', 'dispatch_events',
                    ['action', 'resolved', 'due_at'], unique=False)
    # Hot path: ASHA/admin dashboards scope calls by region + recency.
    op.create_index('ix_call_sessions_region_started', 'call_sessions',
                    ['region_id', 'started_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_call_sessions_region_started', table_name='call_sessions')
    op.drop_index('ix_dispatch_events_timeout', table_name='dispatch_events')
    op.drop_table('system_status')
    op.drop_index(op.f('ix_region_phone_numbers_phone_number'),
                  table_name='region_phone_numbers')
    op.drop_index(op.f('ix_region_phone_numbers_region_id'),
                  table_name='region_phone_numbers')
    op.drop_table('region_phone_numbers')
