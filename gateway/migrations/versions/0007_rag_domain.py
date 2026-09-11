"""multi-tenant RAG domain

Revision ID: 0007_rag_domain
Revises: 0006_workspaces_lifecycle
"""

from alembic import op

from app.db.models import Base
from app.rag import models as rag_models  # noqa: F401


revision = "0007_rag_domain"
down_revision = "0006_workspaces_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS rag")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    rag_tables = [
        table for table in Base.metadata.sorted_tables if table.schema == "rag"
    ]
    Base.metadata.create_all(op.get_bind(), tables=rag_tables, checkfirst=True)


def downgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS rag CASCADE")
