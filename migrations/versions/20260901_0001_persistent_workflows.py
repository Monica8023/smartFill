"""Create persistent task, execution event, and system settings tables."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260901_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "automation_tasks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("target_origin", sa.String(length=255), nullable=False),
        sa.Column("record_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "system_settings",
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("value_json", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "browser_job_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("target_url", sa.Text(), nullable=False),
        sa.Column("configuration_snapshot", sa.JSON(), nullable=False),
        sa.Column("state_snapshot", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["automation_tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_browser_job_runs_task_id", "browser_job_runs", ["task_id"])
    op.create_index("ix_browser_job_runs_status", "browser_job_runs", ["status"])
    op.create_table(
        "browser_job_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("message", sa.String(length=500), nullable=False),
        sa.Column("field", sa.String(length=200), nullable=True),
        sa.Column("step_index", sa.Integer(), nullable=True),
        sa.Column("step_name", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["browser_job_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "sequence", name="uq_browser_job_events_sequence"),
    )
    op.create_index("ix_browser_job_events_job_id", "browser_job_events", ["job_id"])


def downgrade() -> None:
    op.drop_index("ix_browser_job_events_job_id", table_name="browser_job_events")
    op.drop_table("browser_job_events")
    op.drop_index("ix_browser_job_runs_status", table_name="browser_job_runs")
    op.drop_index("ix_browser_job_runs_task_id", table_name="browser_job_runs")
    op.drop_table("browser_job_runs")
    op.drop_table("system_settings")
    op.drop_table("automation_tasks")
