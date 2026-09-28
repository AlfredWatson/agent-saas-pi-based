"""Persist estimated session and active-context token counts.

Revision ID: 0012_session_token_stats
Revises: 0011_local_agent_providers
"""

from alembic import op
from sqlalchemy import text


revision = "0012_session_token_stats"
down_revision = "0011_local_agent_providers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing session histories must be removed through the public DELETE route
    # first so their Runtime-owned Pi JSONL files are cleaned up as well.
    remaining = op.get_bind().execute(text("SELECT COUNT(*) FROM platform.agent_sessions")).scalar_one()
    if remaining:
        raise RuntimeError(
            f"Remove {remaining} existing Agent session(s) through DELETE /api/v1/sessions/{{id}} before migration 0012"
        )
    # 0001 creates tables from current ORM metadata on fresh installations.
    op.execute("ALTER TABLE platform.agent_sessions ADD COLUMN IF NOT EXISTS total_tokens BIGINT NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE platform.agent_sessions ADD COLUMN IF NOT EXISTS context_tokens BIGINT NOT NULL DEFAULT 0")
    op.execute(
        """DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_agent_sessions_total_tokens_nonnegative'
                           AND conrelid = 'platform.agent_sessions'::regclass) THEN
                ALTER TABLE platform.agent_sessions ADD CONSTRAINT ck_agent_sessions_total_tokens_nonnegative CHECK (total_tokens >= 0);
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_agent_sessions_context_tokens_nonnegative'
                           AND conrelid = 'platform.agent_sessions'::regclass) THEN
                ALTER TABLE platform.agent_sessions ADD CONSTRAINT ck_agent_sessions_context_tokens_nonnegative CHECK (context_tokens >= 0);
            END IF;
        END $$"""
    )


def downgrade() -> None:
    op.execute("ALTER TABLE platform.agent_sessions DROP CONSTRAINT IF EXISTS ck_agent_sessions_context_tokens_nonnegative")
    op.execute("ALTER TABLE platform.agent_sessions DROP CONSTRAINT IF EXISTS ck_agent_sessions_total_tokens_nonnegative")
    op.execute("ALTER TABLE platform.agent_sessions DROP COLUMN IF EXISTS context_tokens")
    op.execute("ALTER TABLE platform.agent_sessions DROP COLUMN IF EXISTS total_tokens")
