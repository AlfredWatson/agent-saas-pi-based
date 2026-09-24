"""Persist local Agent provider endpoints and discovered models.

Revision ID: 0011_local_agent_providers
Revises: 0010_workspace_agent_workbench
"""

from alembic import op


revision = "0011_local_agent_providers"
down_revision = "0010_workspace_agent_workbench"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001 creates tables from current ORM metadata on a fresh installation.
    op.execute("ALTER TABLE platform.provider_bindings ADD COLUMN IF NOT EXISTS base_url VARCHAR(2048)")
    op.execute(
        """CREATE TABLE IF NOT EXISTS platform.provider_binding_models (
            binding_id UUID NOT NULL REFERENCES platform.provider_bindings(id) ON DELETE CASCADE,
            model_id VARCHAR(256) NOT NULL,
            name VARCHAR(256) NOT NULL,
            status VARCHAR(32) NOT NULL,
            context_window INTEGER,
            max_tokens INTEGER,
            reasoning BOOLEAN,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (binding_id, model_id)
        )"""
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS platform.provider_binding_models")
    op.execute("ALTER TABLE platform.provider_bindings DROP COLUMN IF EXISTS base_url")
