"""persist ordered, safe tool call and tool result history

Revision ID: 0005_chat_tool_history
Revises: 0004_runtime_docker_only
"""
from alembic import op

revision = "0005_chat_tool_history"
down_revision = "0004_runtime_docker_only"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001 used Base.metadata.create_all(), so a fresh checkout can already
    # contain these current-model columns before this revision is recorded.
    for statement in (
        "ADD COLUMN IF NOT EXISTS tool_call_id VARCHAR(256)",
        "ADD COLUMN IF NOT EXISTS tool_name VARCHAR(256)",
        "ADD COLUMN IF NOT EXISTS arguments JSONB",
        "ADD COLUMN IF NOT EXISTS result JSONB",
        "ADD COLUMN IF NOT EXISTS is_error BOOLEAN",
        "ADD COLUMN IF NOT EXISTS payload_truncated BOOLEAN NOT NULL DEFAULT false",
    ):
        op.execute(f"ALTER TABLE platform.chat_messages {statement}")
    op.execute("""DO $$ BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'uq_chat_messages_session_sequence'
              AND connamespace = 'platform'::regnamespace
        ) THEN
            ALTER TABLE platform.chat_messages
                ADD CONSTRAINT uq_chat_messages_session_sequence UNIQUE (session_id, sequence);
        END IF;
    END $$""")


def downgrade() -> None:
    op.drop_constraint("uq_chat_messages_session_sequence", "chat_messages", schema="platform", type_="unique")
    for column in ("payload_truncated", "is_error", "result", "arguments", "tool_name", "tool_call_id"):
        op.drop_column("chat_messages", column, schema="platform")
