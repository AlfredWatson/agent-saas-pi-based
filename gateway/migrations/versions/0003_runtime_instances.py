"""dynamic docker runtime instances

Revision ID: 0003_runtime_instances
Revises: 0002_real_provider_smoke
"""
from alembic import op

revision = "0003_runtime_instances"
down_revision = "0002_real_provider_smoke"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE platform.runtime_instances (
        id UUID PRIMARY KEY, user_id UUID NOT NULL UNIQUE REFERENCES platform.users(id),
        backend VARCHAR(32) NOT NULL, container_name VARCHAR(128), container_id VARCHAR(128),
        image VARCHAR(256), host_port INTEGER, state VARCHAR(32) NOT NULL DEFAULT 'stopped',
        last_error TEXT, last_seen_at TIMESTAMPTZ, created_at TIMESTAMPTZ DEFAULT now(),
        updated_at TIMESTAMPTZ DEFAULT now())""")
    op.execute("CREATE INDEX ix_runtime_instances_user_id ON platform.runtime_instances (user_id)")


def downgrade() -> None:
    op.drop_table("runtime_instances", schema="platform")
