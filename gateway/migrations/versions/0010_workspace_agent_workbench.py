"""workspace-scoped providers and mutable session model settings

Revision ID: 0010_workspace_agent_workbench
Revises: 0009_agent_rag_tool
"""
from alembic import op


revision = "0010_workspace_agent_workbench"
down_revision = "0009_agent_rag_tool"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # This release is intentionally deployed after clearing disposable test
    # data.  Removing legacy agent records avoids guessing which Workspace a
    # formerly user-scoped credential should belong to.
    op.execute("DELETE FROM platform.chat_messages")
    op.execute("DELETE FROM platform.agent_runs")
    op.execute("DELETE FROM platform.agent_session_knowledge_bases")
    op.execute("DELETE FROM platform.agent_sessions")
    op.execute("DELETE FROM platform.agent_profiles")
    op.execute("DELETE FROM platform.provider_bindings")

    op.execute("ALTER TABLE platform.provider_bindings ADD COLUMN IF NOT EXISTS workspace_id UUID")
    op.execute("ALTER TABLE platform.agent_profiles ADD COLUMN IF NOT EXISTS workspace_id UUID")
    op.execute("ALTER TABLE platform.agent_sessions ALTER COLUMN profile_id DROP NOT NULL")
    op.execute("ALTER TABLE platform.agent_sessions ADD COLUMN IF NOT EXISTS provider_binding_id UUID")
    op.execute("ALTER TABLE platform.agent_sessions ADD COLUMN IF NOT EXISTS model_id VARCHAR(256)")
    op.execute("ALTER TABLE platform.agent_sessions ADD COLUMN IF NOT EXISTS thinking_level VARCHAR(64)")
    op.execute("ALTER TABLE platform.agent_runs ADD COLUMN IF NOT EXISTS provider_binding_id UUID")
    op.execute("ALTER TABLE platform.agent_runs ADD COLUMN IF NOT EXISTS provider_id VARCHAR(128)")
    op.execute("ALTER TABLE platform.agent_runs ADD COLUMN IF NOT EXISTS model_id VARCHAR(256)")
    op.execute("ALTER TABLE platform.agent_runs ADD COLUMN IF NOT EXISTS thinking_level VARCHAR(64)")

    op.execute("ALTER TABLE platform.provider_bindings ALTER COLUMN workspace_id SET NOT NULL")
    op.execute("ALTER TABLE platform.agent_profiles ALTER COLUMN workspace_id SET NOT NULL")
    op.execute("ALTER TABLE platform.provider_bindings DROP CONSTRAINT IF EXISTS provider_bindings_user_id_provider_id_display_name_key")
    op.execute("ALTER TABLE platform.provider_bindings DROP CONSTRAINT IF EXISTS uq_provider_bindings_workspace_provider_name")
    op.execute("ALTER TABLE platform.provider_bindings ADD CONSTRAINT uq_provider_bindings_workspace_provider_name UNIQUE (workspace_id, provider_id, display_name)")
    op.execute("ALTER TABLE platform.provider_bindings ADD CONSTRAINT fk_provider_bindings_workspace FOREIGN KEY (workspace_id) REFERENCES platform.workspaces(id)")
    op.execute("ALTER TABLE platform.agent_profiles ADD CONSTRAINT fk_agent_profiles_workspace FOREIGN KEY (workspace_id) REFERENCES platform.workspaces(id)")
    op.execute("ALTER TABLE platform.agent_sessions ADD CONSTRAINT fk_agent_sessions_provider_binding FOREIGN KEY (provider_binding_id) REFERENCES platform.provider_bindings(id)")
    op.execute("ALTER TABLE platform.agent_runs ADD CONSTRAINT fk_agent_runs_provider_binding FOREIGN KEY (provider_binding_id) REFERENCES platform.provider_bindings(id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_provider_bindings_workspace_id ON platform.provider_bindings (workspace_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_agent_profiles_workspace_id ON platform.agent_profiles (workspace_id)")


def downgrade() -> None:
    op.execute("ALTER TABLE platform.agent_runs DROP CONSTRAINT IF EXISTS fk_agent_runs_provider_binding")
    op.execute("ALTER TABLE platform.agent_sessions DROP CONSTRAINT IF EXISTS fk_agent_sessions_provider_binding")
    op.execute("ALTER TABLE platform.agent_profiles DROP CONSTRAINT IF EXISTS fk_agent_profiles_workspace")
    op.execute("ALTER TABLE platform.provider_bindings DROP CONSTRAINT IF EXISTS fk_provider_bindings_workspace")
    op.execute("ALTER TABLE platform.provider_bindings DROP CONSTRAINT IF EXISTS uq_provider_bindings_workspace_provider_name")
    op.execute("ALTER TABLE platform.agent_runs DROP COLUMN IF EXISTS thinking_level")
    op.execute("ALTER TABLE platform.agent_runs DROP COLUMN IF EXISTS model_id")
    op.execute("ALTER TABLE platform.agent_runs DROP COLUMN IF EXISTS provider_id")
    op.execute("ALTER TABLE platform.agent_runs DROP COLUMN IF EXISTS provider_binding_id")
    op.execute("ALTER TABLE platform.agent_sessions DROP COLUMN IF EXISTS thinking_level")
    op.execute("ALTER TABLE platform.agent_sessions DROP COLUMN IF EXISTS model_id")
    op.execute("ALTER TABLE platform.agent_sessions DROP COLUMN IF EXISTS provider_binding_id")
    op.execute("ALTER TABLE platform.agent_sessions ALTER COLUMN profile_id SET NOT NULL")
    op.execute("ALTER TABLE platform.agent_profiles DROP COLUMN IF EXISTS workspace_id")
    op.execute("ALTER TABLE platform.provider_bindings DROP COLUMN IF EXISTS workspace_id")
