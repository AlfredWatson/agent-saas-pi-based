"""make runtime instance records Docker-only

Revision ID: 0004_runtime_instances_docker_only
Revises: 0003_runtime_instances
"""
from alembic import op

revision = "0004_runtime_instances_docker_only"
down_revision = "0003_runtime_instances"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE platform.runtime_instances DROP COLUMN IF EXISTS backend")


def downgrade() -> None:
    op.execute("ALTER TABLE platform.runtime_instances ADD COLUMN backend VARCHAR(32) NOT NULL DEFAULT 'docker'")
