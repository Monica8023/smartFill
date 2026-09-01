"""Durable orchestration for imported user records executed through saved workflows."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from enum import StrEnum
from threading import RLock
from typing import Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from smartfill.browser_jobs import (
    BrowserJob,
    BrowserJobCreate,
    BrowserJobManager,
    BrowserJobStatus,
    EntryActionConfig,
    SubmissionConfig,
    WorkflowStep,
)
from smartfill.field_schema import FieldDefinition
from smartfill.importing import ImportPreview, ProfileRecord
from smartfill.secrets import SecretStore
from smartfill.tasks import TaskService


class BatchStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    NEEDS_ATTENTION = "needs_attention"
    COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    FAILED = "failed"


class BatchItemStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    NEEDS_ATTENTION = "needs_attention"


class BatchItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: str(uuid4()))
    row_number: int = Field(ge=2)
    status: BatchItemStatus = BatchItemStatus.QUEUED
    browser_job_id: str | None = None
    error_message: str | None = Field(default=None, max_length=500)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class BatchRun(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    workflow_job_id: str
    workflow_name: str
    name: str = Field(min_length=1, max_length=120)
    source_filename: str = Field(min_length=1, max_length=255)
    status: BatchStatus = BatchStatus.QUEUED
    total_records: int = Field(ge=1)
    completed_records: int = Field(default=0, ge=0)
    failed_records: int = Field(default=0, ge=0)
    mapping_snapshot: dict[str, str]
    items: list[BatchItem]
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    completed_at: datetime | None = None


class BatchRepository(Protocol):
    def save(self, batch: BatchRun) -> BatchRun: ...

    def get(self, batch_id: str) -> BatchRun: ...

    def list(self) -> list[BatchRun]: ...


class InMemoryBatchRepository:
    def __init__(self) -> None:
        self._batches: dict[str, BatchRun] = {}
        self._lock = RLock()

    def save(self, batch: BatchRun) -> BatchRun:
        with self._lock:
            self._batches[batch.id] = batch
        return batch

    def get(self, batch_id: str) -> BatchRun:
        with self._lock:
            return self._batches[batch_id]

    def list(self) -> list[BatchRun]:
        with self._lock:
            return sorted(
                self._batches.values(), key=lambda item: item.created_at, reverse=True
            )


class BatchManager:
    """Run imported records sequentially so browser load and side effects stay bounded."""

    def __init__(
        self,
        *,
        repository: BatchRepository,
        browser_jobs: BrowserJobManager,
        task_service: TaskService,
        secret_store: SecretStore,
    ) -> None:
        self._repository = repository
        self._browser_jobs = browser_jobs
        self._task_service = task_service
        self._secret_store = secret_store
        self._records: dict[str, list[ProfileRecord]] = {}
        self._templates: dict[str, BrowserJob] = {}
        self._running: dict[str, asyncio.Task[None]] = {}

    def create(
        self,
        *,
        name: str,
        task_id: str,
        template: BrowserJob,
        preview: ImportPreview,
        mapping: dict[str, str],
    ) -> BatchRun:
        batch = BatchRun(
            task_id=task_id,
            workflow_job_id=template.id,
            workflow_name=template.name,
            name=name.strip(),
            source_filename=preview.filename,
            total_records=preview.total_rows,
            mapping_snapshot=dict(mapping),
            items=[BatchItem(row_number=index) for index in range(2, preview.total_rows + 2)],
        )
        self._records[batch.id] = preview.records
        self._templates[batch.id] = template
        return self._repository.save(batch)

    def start(self, batch_id: str) -> None:
        self.get(batch_id)
        task = asyncio.create_task(self.run(batch_id), name=f"batch-{batch_id}")
        self._running[batch_id] = task
        task.add_done_callback(lambda completed: self._forget(batch_id, completed))

    def _forget(self, batch_id: str, completed: asyncio.Task[None]) -> None:
        if self._running.get(batch_id) is completed:
            self._running.pop(batch_id, None)

    def get(self, batch_id: str) -> BatchRun:
        return self._repository.get(batch_id)

    def list(self) -> list[BatchRun]:
        return self._repository.list()

    async def wait(self, batch_id: str) -> None:
        task = self._running.get(batch_id)
        if task is not None:
            await asyncio.shield(task)

    async def run(self, batch_id: str) -> None:
        batch = self.get(batch_id)
        now = datetime.now(UTC)
        batch = self._save(batch, status=BatchStatus.RUNNING, started_at=now)
        records = self._records[batch_id]
        template = self._templates[batch_id]
        for index, record in enumerate(records):
            batch = self.get(batch_id)
            item = batch.items[index].model_copy(
                update={"status": BatchItemStatus.RUNNING, "updated_at": datetime.now(UTC)}
            )
            batch = self._replace_item(batch, index, item)
            try:
                payload = self._build_payload(batch.task_id, template, record)
                browser_job = self._browser_jobs.create(
                    payload,
                    task_name=f"{batch.name} · 第 {item.row_number} 行",
                )
                item = item.model_copy(
                    update={"browser_job_id": browser_job.id, "updated_at": datetime.now(UTC)}
                )
                batch = self._replace_item(batch, index, item)
                await self._browser_jobs.run(browser_job.id)
                result = self._browser_jobs.get(browser_job.id)
                if result.status is BrowserJobStatus.COMPLETED:
                    item = item.model_copy(
                        update={
                            "status": BatchItemStatus.COMPLETED,
                            "updated_at": datetime.now(UTC),
                        }
                    )
                    batch = self._replace_item(batch, index, item)
                    batch = self._save(
                        batch, completed_records=batch.completed_records + 1
                    )
                elif result.status is BrowserJobStatus.NEED_HUMAN:
                    item = item.model_copy(
                        update={
                            "status": BatchItemStatus.NEEDS_ATTENTION,
                            "error_message": "需要人工确认后再继续批次",
                            "updated_at": datetime.now(UTC),
                        }
                    )
                    batch = self._replace_item(batch, index, item)
                    self._save(batch, status=BatchStatus.NEEDS_ATTENTION)
                    self._task_service.pause(batch.task_id)
                    return
                else:
                    batch = self._mark_failed(batch, index, "浏览器任务执行失败")
            except (KeyError, TypeError, ValueError):
                batch = self._mark_failed(batch, index, "导入记录无法生成工作流输入")

        batch = self.get(batch_id)
        final_status = (
            BatchStatus.COMPLETED
            if batch.failed_records == 0
            else BatchStatus.COMPLETED_WITH_ERRORS
        )
        self._save(batch, status=final_status, completed_at=datetime.now(UTC))
        self._task_service.complete(batch.task_id)

    def _mark_failed(self, batch: BatchRun, index: int, message: str) -> BatchRun:
        item = batch.items[index].model_copy(
            update={
                "status": BatchItemStatus.FAILED,
                "error_message": message,
                "updated_at": datetime.now(UTC),
            }
        )
        batch = self._replace_item(batch, index, item)
        return self._save(batch, failed_records=batch.failed_records + 1)

    def _replace_item(self, batch: BatchRun, index: int, item: BatchItem) -> BatchRun:
        items = list(batch.items)
        items[index] = item
        return self._save(batch, items=items)

    def _save(self, batch: BatchRun, **updates: object) -> BatchRun:
        updates["updated_at"] = datetime.now(UTC)
        return self._repository.save(batch.model_copy(update=updates))

    def _build_payload(
        self,
        task_id: str,
        template: BrowserJob,
        record: ProfileRecord,
    ) -> BrowserJobCreate:
        values = dict(record.values)
        values.update(
            {
                key: self._secret_store.resolve(reference)
                for key, reference in record.secret_refs.items()
            }
        )
        raw_steps = template.configuration_snapshot.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise ValueError("Workflow snapshot has no steps")
        steps: list[WorkflowStep] = []
        for raw_step in raw_steps:
            definitions = [
                FieldDefinition.model_validate(item)
                for item in raw_step["field_definitions"]
            ]
            fields = {
                definition.key: values[definition.key]
                for definition in definitions
                if definition.source_field is None
            }
            steps.append(
                WorkflowStep(
                    id=raw_step["id"],
                    name=raw_step["name"],
                    target_url=raw_step["target_url"],
                    fields=fields,
                    field_definitions=definitions,
                    entry_action=EntryActionConfig.model_validate(raw_step["entry_action"]),
                    submission=SubmissionConfig.model_validate(raw_step["submission"]),
                )
            )
        return BrowserJobCreate(
            task_id=task_id,
            target_url=steps[0].target_url,
            workflow_steps=steps,
        )
