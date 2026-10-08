"""initial platform tables

Revision ID: 0001_initial
"""

from alembic import op
from app.db.models import Base

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS platform")
    bind = op.get_bind()
    # The RAG schema is introduced in 0007. Its session binding table is
    # created by 0009 after the RAG rebuild, when the referenced KB exists.
    platform_tables = [
        table
        for table in Base.metadata.sorted_tables
        if table.schema == "platform"
        and table.name != "agent_session_knowledge_bases"
    ]
    Base.metadata.create_all(bind, tables=platform_tables, checkfirst=True)


def downgrade() -> None:
    platform_tables = [
        table for table in Base.metadata.sorted_tables if table.schema == "platform"
    ]
    Base.metadata.drop_all(op.get_bind(), tables=platform_tables, checkfirst=True)
