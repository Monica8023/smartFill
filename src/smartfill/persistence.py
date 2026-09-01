"""SQLAlchemy repositories for durable task runs and runtime configuration."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    delete,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Engine

from smartfill.batch_jobs import BatchItem, BatchRepository, BatchRun
from smartfill.browser_jobs import BrowserJob, JobEvent
from smartfill.domain import Task
from smartfill.tasks import TaskNotFoundError

metadata = MetaData()

automation_tasks = Table(
    "automation_tasks",
    metadata,
    # Logical task used by the control-plane state machine.
    Column("id", String(36), primary_key=True),
    Column("name", String(120), nullable=False),
    Column("target_origin", String(255), nullable=False),
    Column("record_count", Integer, nullable=False),
    Column("status", String(32), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

browser_job_runs = Table(
    "browser_job_runs",
    metadata,
    Column("id", String(36), primary_key=True),
    Column(
        "task_id", String(36), ForeignKey("automation_tasks.id"), nullable=False, index=True
    ),
    Column("name", String(120), nullable=False),
    Column("status", String(32), nullable=False, index=True),
    Column("target_url", Text, nullable=False),
    Column("configuration_snapshot", JSON, nullable=False),
    Column("state_snapshot", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

browser_job_events = Table(
    "browser_job_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column(
        "job_id", String(36), ForeignKey("browser_job_runs.id"), nullable=False, index=True
    ),
    Column("sequence", Integer, nullable=False),
    Column("status", String(32), nullable=False),
    Column("message", String(500), nullable=False),
    Column("field", String(200), nullable=True),
    Column("step_index", Integer, nullable=True),
    Column("step_name", String(120), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("job_id", "sequence", name="uq_browser_job_events_sequence"),
)

system_settings = Table(
    "system_settings",
    metadata,
    Column("key", String(100), primary_key=True),
    Column("value_json", JSON, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

batch_runs = Table(
    "batch_runs",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("task_id", String(36), ForeignKey("automation_tasks.id"), nullable=False, index=True),
    Column(
        "workflow_job_id",
        String(36),
        ForeignKey("browser_job_runs.id"),
        nullable=False,
        index=True,
    ),
    Column("workflow_name", String(120), nullable=False),
    Column("name", String(120), nullable=False),
    Column("source_filename", String(255), nullable=False),
    Column("status", String(32), nullable=False, index=True),
    Column("total_records", Integer, nullable=False),
    Column("completed_records", Integer, nullable=False),
    Column("failed_records", Integer, nullable=False),
    Column("mapping_snapshot", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=True),
    Column("completed_at", DateTime(timezone=True), nullable=True),
)

batch_items = Table(
    "batch_items",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("batch_id", String(36), ForeignKey("batch_runs.id"), nullable=False, index=True),
    Column("row_number", Integer, nullable=False),
    Column("status", String(32), nullable=False, index=True),
    Column("browser_job_id", String(36), ForeignKey("browser_job_runs.id"), nullable=True),
    Column("error_message", String(500), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("batch_id", "row_number", name="uq_batch_items_row_number"),
)


def create_schema(engine: Engine) -> None:
    """Create tables for isolated tests; deployments use the Alembic migration."""

    metadata.create_all(engine)


class SqlAlchemyTaskRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def save(self, task: Task) -> Task:
        payload = task.model_dump(mode="python")
        with self._engine.begin() as connection:
            exists = connection.execute(
                select(automation_tasks.c.id).where(automation_tasks.c.id == task.id)
            ).first()
            if exists:
                connection.execute(
                    update(automation_tasks)
                    .where(automation_tasks.c.id == task.id)
                    .values(**payload)
                )
            else:
                connection.execute(insert(automation_tasks).values(**payload))
        return task

    def get(self, task_id: str) -> Task:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(automation_tasks).where(automation_tasks.c.id == task_id)
            ).mappings().first()
        if row is None:
            raise TaskNotFoundError(task_id)
        return Task.model_validate(dict(row))

    def list(self) -> list[Task]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(automation_tasks).order_by(automation_tasks.c.created_at.desc())
            ).mappings()
            return [Task.model_validate(dict(row)) for row in rows]


class SqlAlchemyBrowserJobRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def save(self, job: BrowserJob) -> BrowserJob:
        state = job.model_dump(mode="json", exclude={"events", "configuration_snapshot"})
        row = {
            "id": job.id,
            "task_id": job.task_id,
            "name": job.name,
            "status": job.status.value,
            "target_url": job.target_url,
            "configuration_snapshot": job.configuration_snapshot,
            "state_snapshot": state,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
        }
        with self._engine.begin() as connection:
            exists = connection.execute(
                select(browser_job_runs.c.id).where(browser_job_runs.c.id == job.id)
            ).first()
            if exists:
                connection.execute(
                    update(browser_job_runs)
                    .where(browser_job_runs.c.id == job.id)
                    .values(**{key: value for key, value in row.items() if key != "id"})
                )
            else:
                connection.execute(insert(browser_job_runs).values(**row))
            connection.execute(
                delete(browser_job_events).where(browser_job_events.c.job_id == job.id)
            )
            if job.events:
                connection.execute(
                    insert(browser_job_events),
                    [
                        {
                            "job_id": job.id,
                            **event.model_dump(mode="python"),
                            "status": event.status.value,
                        }
                        for event in job.events
                    ],
                )
        return job

    def get(self, job_id: str) -> BrowserJob:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(browser_job_runs).where(browser_job_runs.c.id == job_id)
            ).mappings().first()
            if row is None:
                raise KeyError(job_id)
            events = connection.execute(
                select(browser_job_events)
                .where(browser_job_events.c.job_id == job_id)
                .order_by(browser_job_events.c.sequence)
            ).mappings()
            event_models = [
                JobEvent.model_validate(
                    {
                        key: value
                        for key, value in dict(event).items()
                        if key not in {"id", "job_id"}
                    }
                )
                for event in events
            ]
        state = dict(row["state_snapshot"])
        state["configuration_snapshot"] = row["configuration_snapshot"]
        state["events"] = event_models
        return BrowserJob.model_validate(state)

    def list(self) -> list[BrowserJob]:
        with self._engine.connect() as connection:
            ids = connection.execute(
                select(browser_job_runs.c.id).order_by(browser_job_runs.c.created_at.desc())
            ).scalars()
            job_ids = list(ids)
        return [self.get(job_id) for job_id in job_ids]


class SqlAlchemyBatchRepository(BatchRepository):
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def save(self, batch: BatchRun) -> BatchRun:
        row = batch.model_dump(mode="python", exclude={"items"})
        row["status"] = batch.status.value
        with self._engine.begin() as connection:
            exists = connection.execute(
                select(batch_runs.c.id).where(batch_runs.c.id == batch.id)
            ).first()
            if exists:
                connection.execute(
                    update(batch_runs)
                    .where(batch_runs.c.id == batch.id)
                    .values(**{key: value for key, value in row.items() if key != "id"})
                )
            else:
                connection.execute(insert(batch_runs).values(**row))
            connection.execute(delete(batch_items).where(batch_items.c.batch_id == batch.id))
            if batch.items:
                connection.execute(
                    insert(batch_items),
                    [
                        {
                            "batch_id": batch.id,
                            **item.model_dump(mode="python"),
                            "status": item.status.value,
                        }
                        for item in batch.items
                    ],
                )
        return batch

    def get(self, batch_id: str) -> BatchRun:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(batch_runs).where(batch_runs.c.id == batch_id)
            ).mappings().first()
            if row is None:
                raise KeyError(batch_id)
            items = connection.execute(
                select(batch_items)
                .where(batch_items.c.batch_id == batch_id)
                .order_by(batch_items.c.row_number)
            ).mappings()
            item_models = [
                BatchItem.model_validate(
                    {
                        key: value
                        for key, value in dict(item).items()
                        if key != "batch_id"
                    }
                )
                for item in items
            ]
        return BatchRun.model_validate({**dict(row), "items": item_models})

    def list(self) -> list[BatchRun]:
        with self._engine.connect() as connection:
            ids = connection.execute(
                select(batch_runs.c.id).order_by(batch_runs.c.created_at.desc())
            ).scalars()
            batch_ids = list(ids)
        return [self.get(batch_id) for batch_id in batch_ids]


class TargetOriginSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    origins: list[str]
    updated_at: datetime


class SqlAlchemySystemSettingsRepository:
    TARGET_ORIGINS_KEY = "target_origins"

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def get_target_origins(self) -> TargetOriginSettings | None:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(system_settings).where(
                    system_settings.c.key == self.TARGET_ORIGINS_KEY
                )
            ).mappings().first()
        if row is None:
            return None
        value = row["value_json"]
        updated_at = row["updated_at"]
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=UTC)
        return TargetOriginSettings(origins=list(value["origins"]), updated_at=updated_at)

    def replace_target_origins(self, origins: list[str]) -> TargetOriginSettings:
        now = datetime.now(UTC)
        payload: dict[str, Any] = {"origins": origins}
        with self._engine.begin() as connection:
            exists = connection.execute(
                select(system_settings.c.key).where(
                    system_settings.c.key == self.TARGET_ORIGINS_KEY
                )
            ).first()
            if exists:
                connection.execute(
                    update(system_settings)
                    .where(system_settings.c.key == self.TARGET_ORIGINS_KEY)
                    .values(value_json=payload, updated_at=now)
                )
            else:
                connection.execute(
                    insert(system_settings).values(
                        key=self.TARGET_ORIGINS_KEY,
                        value_json=payload,
                        updated_at=now,
                    )
                )
        return TargetOriginSettings(origins=origins, updated_at=now)
