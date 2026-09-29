"""Configurable session tools and durable subagent sessions.

Revision ID: 0013_subagents
Revises: 0012_session_token_stats
"""

from alembic import op
from sqlalchemy import text

revision = "0013_subagents"
down_revision = "0012_session_token_stats"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE platform.agent_sessions ADD COLUMN IF NOT EXISTS tools JSONB")
    op.execute("ALTER TABLE platform.agent_sessions ADD COLUMN IF NOT EXISTS config_version INTEGER NOT NULL DEFAULT 1")
    op.execute("ALTER TABLE platform.agent_sessions ADD COLUMN IF NOT EXISTS parent_session_id UUID REFERENCES platform.agent_sessions(id)")
    op.execute("ALTER TABLE platform.agent_sessions ADD COLUMN IF NOT EXISTS parent_run_id UUID")
    op.execute("ALTER TABLE platform.agent_sessions ADD COLUMN IF NOT EXISTS parent_tool_call_id VARCHAR(256)")
    op.execute("ALTER TABLE platform.agent_sessions ADD COLUMN IF NOT EXISTS task_index INTEGER")
    op.execute("ALTER TABLE platform.agent_sessions ADD COLUMN IF NOT EXISTS task_input TEXT")
    op.execute("ALTER TABLE platform.agent_sessions ADD COLUMN IF NOT EXISTS subagent_snapshot JSONB")
    op.execute("CREATE INDEX IF NOT EXISTS ix_agent_sessions_parent_session_id ON platform.agent_sessions(parent_session_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_agent_sessions_parent_run_id ON platform.agent_sessions(parent_run_id)")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_subagent_invocation_task ON platform.agent_sessions(parent_run_id, parent_tool_call_id, task_index) WHERE parent_session_id IS NOT NULL")
    op.execute("""CREATE TABLE IF NOT EXISTS platform.agent_subagent_definitions (
        session_id UUID NOT NULL REFERENCES platform.agent_sessions(id) ON DELETE CASCADE,
        name VARCHAR(64) NOT NULL,
        description VARCHAR(500) NOT NULL,
        system_prompt TEXT NOT NULL,
        tools JSONB NOT NULL,
        PRIMARY KEY (session_id, name)
    )""")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS platform.agent_subagent_definitions")
    op.execute("DROP INDEX IF EXISTS platform.uq_subagent_invocation_task")
    op.execute("DROP INDEX IF EXISTS platform.ix_agent_sessions_parent_session_id")
    op.execute("DROP INDEX IF EXISTS platform.ix_agent_sessions_parent_run_id")
    for column in ("subagent_snapshot", "task_input", "task_index", "parent_tool_call_id", "parent_run_id", "parent_session_id", "config_version", "tools"):
        op.execute(text(f"ALTER TABLE platform.agent_sessions DROP COLUMN IF EXISTS {column}"))
