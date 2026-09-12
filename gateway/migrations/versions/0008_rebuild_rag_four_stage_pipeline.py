"""rebuild disposable RAG schema for the four-stage pipeline

Revision ID: 0008_rag_four_stage
Revises: 0007_rag_domain

The user explicitly approved discarding existing RAG test data.  Keep the
destructive operation scoped to `rag`; platform users and workspaces are not
modified.
"""

from alembic import op

from app.db.models import Base
from app.rag import models as rag_models  # noqa: F401


revision = "0008_rag_four_stage"
down_revision = "0007_rag_domain"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS rag CASCADE")
    op.execute("CREATE SCHEMA rag")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    rag_tables = [
        table for table in Base.metadata.sorted_tables if table.schema == "rag"
    ]
    Base.metadata.create_all(op.get_bind(), tables=rag_tables, checkfirst=False)


def downgrade() -> None:
    # This release intentionally has no data-preserving downgrade path.
    op.execute("DROP SCHEMA IF EXISTS rag CASCADE")
