"""bind Agent sessions to RAG knowledge bases and credential Runtime RAG calls

Revision ID: 0009_agent_rag_tool
Revises: 0008_rag_four_stage
"""

from alembic import op


revision = "0009_agent_rag_tool"
down_revision = "0008_rag_four_stage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS platform.agent_session_knowledge_bases (
            session_id UUID NOT NULL REFERENCES platform.agent_sessions(id) ON DELETE CASCADE,
            knowledge_base_id UUID NOT NULL REFERENCES rag.knowledge_bases(id) ON DELETE CASCADE,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            CONSTRAINT pk_agent_session_knowledge_bases PRIMARY KEY (session_id, knowledge_base_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_agent_session_knowledge_bases_knowledge_base_id "
        "ON platform.agent_session_knowledge_bases (knowledge_base_id)"
    )
    op.execute(
        "ALTER TABLE platform.runtime_instances "
        "ADD COLUMN IF NOT EXISTS rag_secret_digest BYTEA"
    )


def downgrade() -> None:
    op.drop_table("agent_session_knowledge_bases", schema="platform")
    op.drop_column("runtime_instances", "rag_secret_digest", schema="platform")
