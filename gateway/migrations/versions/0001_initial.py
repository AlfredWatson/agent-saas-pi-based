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
    Base.metadata.create_all(bind, checkfirst=True)

def downgrade() -> None:
    Base.metadata.drop_all(op.get_bind(), checkfirst=True)
