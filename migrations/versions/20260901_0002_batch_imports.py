"""Create durable batch import progress tables."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260901_0002"
down_revision: str | None = "20260901_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "batch_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("workflow_job_id", sa.String(length=36), nullable=False),
        sa.Column("workflow_name", sa.String(length=120), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("source_filename", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("total_records", sa.Integer(), nullable=False),
        sa.Column("completed_records", sa.Integer(), nullable=False),
        sa.Column("failed_records", sa.Integer(), nullable=False),
        sa.Column("mapping_snapshot", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["task_id"], ["automation_tasks.id"]),
        sa.ForeignKeyConstraint(["workflow_job_id"], ["browser_job_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_batch_runs_task_id", "batch_runs", ["task_id"])
    op.create_index("ix_batch_runs_workflow_job_id", "batch_runs", ["workflow_job_id"])
    op.create_index("ix_batch_runs_status", "batch_runs", ["status"])
    op.create_table(
        "batch_items",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("browser_job_id", sa.String(length=36), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["batch_id"], ["batch_runs.id"]),
        sa.ForeignKeyConstraint(["browser_job_id"], ["browser_job_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_id", "row_number", name="uq_batch_items_row_number"),
    )
    op.create_index("ix_batch_items_batch_id", "batch_items", ["batch_id"])
    op.create_index("ix_batch_items_status", "batch_items", ["status"])


def downgrade() -> None:
    op.drop_index("ix_batch_items_status", table_name="batch_items")
    op.drop_index("ix_batch_items_batch_id", table_name="batch_items")
    op.drop_table("batch_items")
    op.drop_index("ix_batch_runs_status", table_name="batch_runs")
    op.drop_index("ix_batch_runs_workflow_job_id", table_name="batch_runs")
    op.drop_index("ix_batch_runs_task_id", table_name="batch_runs")
    op.drop_table("batch_runs")
