"""real provider smoke support without destructive prototype migration

Revision ID: 0002_real_provider_smoke
Revises: 0001_initial
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_real_provider_smoke"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

def upgrade() -> None:
    # 0001 of the prototype created tables in public even though it created the
    # platform schema. Move them atomically instead of recreating or deleting
    # user data; fresh installs already have them in platform.
    for table in ("users", "workspaces", "provider_bindings", "agent_profiles", "agent_sessions", "chat_messages"):
        op.execute(sa.text(f'''DO $$ BEGIN
            IF to_regclass('platform.{table}') IS NULL AND to_regclass('public.{table}') IS NOT NULL THEN
                EXECUTE 'ALTER TABLE public.{table} SET SCHEMA platform';
            END IF;
        END $$'''))
    op.execute("ALTER TABLE platform.provider_bindings ADD COLUMN IF NOT EXISTS verified_at TIMESTAMP WITH TIME ZONE")
    op.execute("""CREATE TABLE IF NOT EXISTS platform.agent_runs (
        id UUID PRIMARY KEY, session_id UUID NOT NULL REFERENCES platform.agent_sessions(id),
        user_id UUID NOT NULL REFERENCES platform.users(id), status VARCHAR(32) NOT NULL DEFAULT 'running',
        idempotency_key VARCHAR(128), error TEXT, started_at TIMESTAMPTZ DEFAULT now(), finished_at TIMESTAMPTZ)""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_agent_runs_session_id ON platform.agent_runs (session_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_agent_runs_user_id ON platform.agent_runs (user_id)")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_runs_one_running_session ON platform.agent_runs (session_id) WHERE status = 'running'")
    op.execute("ALTER TABLE platform.chat_messages ADD COLUMN IF NOT EXISTS run_id UUID REFERENCES platform.agent_runs(id)")

def downgrade() -> None:
    op.drop_column("chat_messages", "run_id", schema="platform")
    op.drop_table("agent_runs", schema="platform")
    op.drop_column("provider_bindings", "verified_at", schema="platform")
