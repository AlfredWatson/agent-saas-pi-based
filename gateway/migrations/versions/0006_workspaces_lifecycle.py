"""workspace lifecycle and limits

Revision ID: 0006_workspaces_lifecycle
Revises: 0005_chat_tool_history
"""
from alembic import op


revision = "0006_workspaces_lifecycle"
down_revision = "0005_chat_tool_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE platform.workspaces ADD COLUMN IF NOT EXISTS is_current BOOLEAN NOT NULL DEFAULT false")
    op.execute("ALTER TABLE platform.workspaces DROP CONSTRAINT IF EXISTS workspaces_storage_key_key")
    op.execute("""DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_workspaces_user_name' AND connamespace = 'platform'::regnamespace) THEN
            ALTER TABLE platform.workspaces ADD CONSTRAINT uq_workspaces_user_name UNIQUE (user_id, name);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_workspaces_user_storage_key' AND connamespace = 'platform'::regnamespace) THEN
            ALTER TABLE platform.workspaces ADD CONSTRAINT uq_workspaces_user_storage_key UNIQUE (user_id, storage_key);
        END IF;
    END $$""")
    op.execute("""WITH ranked AS (
        SELECT id, row_number() OVER (PARTITION BY user_id ORDER BY created_at, id) AS position
        FROM platform.workspaces
    )
    UPDATE platform.workspaces AS workspace
    SET is_current = true
    FROM ranked
    WHERE workspace.id = ranked.id AND ranked.position = 1
      AND NOT EXISTS (SELECT 1 FROM platform.workspaces existing WHERE existing.user_id = workspace.user_id AND existing.is_current)""")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_workspaces_one_current ON platform.workspaces (user_id) WHERE is_current")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS platform.uq_workspaces_one_current")
    op.execute("ALTER TABLE platform.workspaces DROP CONSTRAINT IF EXISTS uq_workspaces_user_storage_key")
    op.execute("ALTER TABLE platform.workspaces DROP CONSTRAINT IF EXISTS uq_workspaces_user_name")
    op.execute("ALTER TABLE platform.workspaces ADD CONSTRAINT workspaces_storage_key_key UNIQUE (storage_key)")
    op.execute("ALTER TABLE platform.workspaces DROP COLUMN IF EXISTS is_current")
