"""Browser automation job models, storage, progress streaming, and secret handling."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import suppress
from datetime import UTC, datetime
from enum import StrEnum
from threading import RLock
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from smartfill.field_schema import (
    FIELD_KEY_PATTERN,
    FieldDefinition,
    resolve_field_definitions,
)
from smartfill.secrets import SecretStore


class BrowserJobStatus(StrEnum):
    QUEUED = "queued"
    STARTING = "starting"
    NAVIGATING = "navigating"
    ENTERING = "entering"
    OBSERVING = "observing"
    FILLING = "filling"
    VERIFYING = "verifying"
    SUBMITTING = "submitting"
    NEED_HUMAN = "need_human"
    RESUMING = "resuming"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SubmissionPolicy(StrEnum):
    FILL_ONLY = "fill_only"
    CONFIRM_BEFORE_SUBMIT = "confirm_before_submit"
    AUTO_SUBMIT = "auto_submit"


class EntryActionMode(StrEnum):
    DIRECT = "direct"
    CLICK = "click"


class EntryActionConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    mode: EntryActionMode = EntryActionMode.DIRECT
    aliases: list[str] = Field(
        default_factory=lambda: ["登录", "登陆", "Login", "Sign in"],
        max_length=20,
    )

    @field_validator("aliases")
    @classmethod
    def validate_aliases(cls, values: list[str]) -> list[str]:
        aliases: list[str] = []
        seen: set[str] = set()
        for value in values:
            alias = value.strip()
            if not alias or len(alias) > 100 or any(ord(char) < 32 for char in alias):
                raise ValueError("Invalid entry action alias")
            folded = alias.casefold()
            if folded not in seen:
                aliases.append(alias)
                seen.add(folded)
        return aliases

    @model_validator(mode="after")
    def require_aliases_for_click(self) -> EntryActionConfig:
        if self.mode is EntryActionMode.CLICK and not self.aliases:
            raise ValueError("Entry action aliases are required")
        return self


class SubmissionConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    policy: SubmissionPolicy = SubmissionPolicy.FILL_ONLY
    button_aliases: list[str] = Field(
        default_factory=lambda: [
            "提交",
            "保存",
            "登录",
            "注册",
            "submit",
            "save",
            "login",
            "register",
        ],
        max_length=20,
    )

    @field_validator("button_aliases")
    @classmethod
    def validate_button_aliases(cls, values: list[str]) -> list[str]:
        aliases: list[str] = []
        for value in values:
            alias = value.strip()
            if not alias or len(alias) > 100 or any(ord(char) < 32 for char in alias):
                raise ValueError("Invalid submission button alias")
            if alias not in aliases:
                aliases.append(alias)
        return aliases

    @model_validator(mode="after")
    def require_aliases_for_submission(self) -> SubmissionConfig:
        if self.policy is not SubmissionPolicy.FILL_ONLY and not self.button_aliases:
            raise ValueError("Submission button aliases are required")
        return self


class WorkflowStep(BaseModel):
    """One form interaction executed inside the workflow's shared browser context."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=120)
    target_url: str = Field(min_length=1, max_length=2_048)
    fields: dict[str, str] = Field(min_length=1, max_length=30)
    field_definitions: list[FieldDefinition] = Field(default_factory=list, max_length=30)
    submission: SubmissionConfig = Field(default_factory=SubmissionConfig)
    entry_action: EntryActionConfig = Field(default_factory=EntryActionConfig)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(char) < 32 for char in value):
            raise ValueError("Workflow step name cannot be blank")
        return value

    @field_validator("target_url")
    @classmethod
    def validate_target_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("A valid HTTP(S) target URL is required")
        if parsed.username or parsed.password:
            raise ValueError("Target URL cannot contain credentials")
        return value

    @field_validator("fields")
    @classmethod
    def validate_fields(cls, values: dict[str, str]) -> dict[str, str]:
        for canonical, value in values.items():
            if FIELD_KEY_PATTERN.fullmatch(canonical) is None:
                raise ValueError(f"Invalid field key: {canonical}")
            if not value or len(value) > 4_096:
                raise ValueError(f"Invalid value for field: {canonical}")
        return values

    @model_validator(mode="after")
    def prepare_dynamic_fields(self) -> WorkflowStep:
        fields, definitions = resolve_field_definitions(self.fields, self.field_definitions)
        object.__setattr__(self, "fields", fields)
        object.__setattr__(self, "field_definitions", definitions)
        return self


class BrowserJobCreate(BaseModel):
    task_id: str = Field(min_length=1, max_length=100)
    target_url: str = Field(min_length=1, max_length=2_048)
    fields: dict[str, str] = Field(default_factory=dict, max_length=30)
    field_definitions: list[FieldDefinition] = Field(default_factory=list, max_length=30)
    submission: SubmissionConfig = Field(default_factory=SubmissionConfig)
    entry_action: EntryActionConfig = Field(default_factory=EntryActionConfig)
    workflow_steps: list[WorkflowStep] = Field(default_factory=list, max_length=10)

    @field_validator("target_url")
    @classmethod
    def validate_target_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("A valid HTTP(S) target URL is required")
        if parsed.username or parsed.password:
            raise ValueError("Target URL cannot contain credentials")
        return value

    @field_validator("fields")
    @classmethod
    def validate_fields(cls, values: dict[str, str]) -> dict[str, str]:
        for canonical, value in values.items():
            if FIELD_KEY_PATTERN.fullmatch(canonical) is None:
                raise ValueError(f"Invalid field key: {canonical}")
            if not value or len(value) > 4_096:
                raise ValueError(f"Invalid value for field: {canonical}")
        return values

    @model_validator(mode="after")
    def prepare_dynamic_fields(self) -> BrowserJobCreate:
        if self.workflow_steps:
            if self.fields or self.field_definitions:
                raise ValueError("Workflow jobs cannot also define top-level fields")
            return self
        if not self.fields:
            raise ValueError("At least one field or workflow step is required")
        self.fields, self.field_definitions = resolve_field_definitions(
            self.fields,
            self.field_definitions,
        )
        return self


class BrowserRunRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    job_id: str
    task_id: str
    target_url: str
    fields: dict[str, str]
    field_definitions: list[FieldDefinition] = Field(default_factory=list, max_length=30)
    submission: SubmissionConfig = Field(default_factory=SubmissionConfig)
    entry_action: EntryActionConfig = Field(default_factory=EntryActionConfig)
    workflow_steps: list[WorkflowStep] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def prepare_dynamic_fields(self) -> BrowserRunRequest:
        fields_to_resolve = dict(self.fields)
        for definition in self.field_definitions:
            source = definition.source_field
            if (
                source is not None
                and definition.key in fields_to_resolve
                and fields_to_resolve.get(definition.key) == fields_to_resolve.get(source)
            ):
                fields_to_resolve.pop(definition.key)
        fields, definitions = resolve_field_definitions(
            fields_to_resolve,
            self.field_definitions,
        )
        object.__setattr__(self, "fields", fields)
        object.__setattr__(self, "field_definitions", definitions)
        return self


class InterventionKind(StrEnum):
    FIELD_MAPPING = "field_mapping"
    HUMAN_CHALLENGE = "human_challenge"
    VERIFICATION = "verification"
    SUBMISSION_CONFIRMATION = "submission_confirmation"
    ENTRY_ACTION_CONFIRMATION = "entry_action_confirmation"


class InterventionCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    element_id: str = Field(min_length=1, max_length=200)
    accessible_name: str = Field(default="", max_length=300)
    role: str = Field(default="", max_length=80)
    tag: str = Field(default="", max_length=40)
    frame_path: str = Field(default="main", max_length=300)
    confidence: float = Field(default=0, ge=0, le=1)


class FieldCandidateSet(BaseModel):
    model_config = ConfigDict(frozen=True)

    canonical_field: str
    candidates: list[InterventionCandidate] = Field(default_factory=list, max_length=20)


class HumanIntervention(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: InterventionKind
    instruction: str = Field(min_length=1, max_length=500)
    field_candidates: list[FieldCandidateSet] = Field(default_factory=list, max_length=30)
    submission_candidates: list[InterventionCandidate] = Field(
        default_factory=list,
        max_length=20,
    )
    entry_candidates: list[InterventionCandidate] = Field(
        default_factory=list,
        max_length=20,
    )
    requires_browser_interaction: bool = False


class HumanResolution(BaseModel):
    model_config = ConfigDict(frozen=True)

    field_mappings: dict[str, str] = Field(default_factory=dict, max_length=30)
    approve_submission: bool = False
    submit_element_id: str | None = Field(default=None, max_length=200)
    approve_entry_action: bool = False
    entry_element_id: str | None = Field(default=None, max_length=200)

    @field_validator("field_mappings")
    @classmethod
    def validate_mapping_fields(cls, values: dict[str, str]) -> dict[str, str]:
        for canonical, element_id in values.items():
            if FIELD_KEY_PATTERN.fullmatch(canonical) is None:
                raise ValueError(f"Invalid field key: {canonical}")
            if not element_id or len(element_id) > 200:
                raise ValueError("Invalid intervention candidate id")
        return values


class JobProgress(BaseModel):
    status: BrowserJobStatus
    message: str = Field(max_length=500)
    current_url: str | None = None
    current_field: str | None = None
    completed_fields: int = Field(default=0, ge=0)
    screenshot_path: str | None = None
    intervention: HumanIntervention | None = None
    submitted: bool = False
    entry_action_performed: bool = False
    current_step: int | None = Field(default=None, ge=1)
    current_step_name: str | None = Field(default=None, max_length=120)


class BrowserRunResult(JobProgress):
    pass


class PageScanRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    target_url: str = Field(min_length=1, max_length=2_048)
    entry_action: EntryActionConfig = Field(default_factory=EntryActionConfig)

    @field_validator("target_url")
    @classmethod
    def validate_target_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("A valid HTTP(S) target URL is required")
        if parsed.username or parsed.password:
            raise ValueError("Target URL cannot contain credentials")
        return value


class PageScanResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    initial_url: str
    final_url: str
    entry_action_performed: bool = False
    fields: list[FieldDefinition] = Field(default_factory=list, max_length=30)


class BrowserAutomationWorker(Protocol):
    async def scan_page(self, request: PageScanRequest) -> PageScanResult:
        """Discover a form schema without reading or submitting field values."""

    async def run(
        self,
        request: BrowserRunRequest,
        report: Callable[[JobProgress], Awaitable[None]],
    ) -> BrowserRunResult:
        """Execute one isolated browser job."""

    async def resume(
        self,
        job_id: str,
        resolution: HumanResolution,
        report: Callable[[JobProgress], Awaitable[None]],
    ) -> BrowserRunResult:
        """Resume a paused browser session after a validated human decision."""

    async def cancel(self, job_id: str) -> None:
        """Release resources retained by a paused browser job."""


class JobEvent(BaseModel):
    sequence: int
    status: BrowserJobStatus
    message: str
    field: str | None = None
    step_index: int | None = Field(default=None, ge=1)
    step_name: str | None = Field(default=None, max_length=120)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class BrowserJob(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    name: str = "浏览器自动化任务"
    target_url: str
    field_names: list[str]
    status: BrowserJobStatus = BrowserJobStatus.QUEUED
    message: str = "等待 Browser Worker"
    current_url: str | None = None
    current_field: str | None = None
    completed_fields: int = 0
    total_fields: int
    submission_policy: SubmissionPolicy = SubmissionPolicy.FILL_ONLY
    submitted: bool = False
    entry_action_mode: EntryActionMode = EntryActionMode.DIRECT
    entry_action_performed: bool = False
    current_step: int = Field(default=1, ge=1)
    current_step_name: str = "表单填写"
    total_steps: int = Field(default=1, ge=1, le=10)
    configuration_snapshot: dict[str, Any] = Field(default_factory=dict)
    screenshot_url: str | None = None
    intervention: HumanIntervention | None = None
    events: list[JobEvent] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class BrowserJobNotFoundError(LookupError):
    pass


class BrowserJobRepository(Protocol):
    def save(self, job: BrowserJob) -> BrowserJob:
        """Persist a sanitized browser job and its append-only events."""

    def get(self, job_id: str) -> BrowserJob:
        """Return a persisted browser job."""

    def list(self) -> list[BrowserJob]:
        """Return persisted jobs newest first."""


class BrowserJobManager:
    """Coordinate a worker while exposing only sanitized job state."""

    def __init__(
        self,
        worker: BrowserAutomationWorker,
        secret_store: SecretStore,
        repository: BrowserJobRepository | None = None,
    ) -> None:
        self._worker = worker
        self._secret_store = secret_store
        self._repository = repository
        restored = repository.list() if repository is not None else []
        self._jobs: dict[str, BrowserJob] = {job.id: job for job in restored}
        self._requests: dict[str, BrowserRunRequest] = {}
        self._screenshot_paths: dict[str, str] = {}
        self._subscribers: dict[str, set[asyncio.Queue[BrowserJob]]] = {
            job.id: set() for job in restored
        }
        self._running: dict[str, asyncio.Task[None]] = {}
        self._lock = RLock()

    def create(self, payload: BrowserJobCreate, *, task_name: str | None = None) -> BrowserJob:
        direct_definitions = {
            definition.key: definition for definition in payload.field_definitions
        }
        direct_fields = {
            key: value
            for key, value in payload.fields.items()
            if direct_definitions[key].source_field is None
        }
        source_steps = payload.workflow_steps or [
            WorkflowStep(
                name="表单填写",
                target_url=payload.target_url,
                fields=direct_fields,
                field_definitions=payload.field_definitions,
                submission=payload.submission,
                entry_action=payload.entry_action,
            )
        ]
        protected_steps = [self._protect_step(payload.task_id, step) for step in source_steps]
        all_field_names = [key for step in source_steps for key in step.fields]
        job = BrowserJob(
            task_id=payload.task_id,
            name=task_name or "浏览器自动化任务",
            target_url=self._safe_display_url(payload.target_url),
            field_names=all_field_names,
            total_fields=len(all_field_names),
            submission_policy=source_steps[-1].submission.policy,
            entry_action_mode=source_steps[0].entry_action.mode,
            current_step_name=source_steps[0].name,
            total_steps=len(source_steps),
            configuration_snapshot=self._configuration_snapshot(source_steps),
            events=[
                JobEvent(
                    sequence=1,
                    status=BrowserJobStatus.QUEUED,
                    message="任务已进入 Browser Worker 队列",
                    step_index=1,
                    step_name=source_steps[0].name,
                )
            ],
        )
        first_step = protected_steps[0]
        request = BrowserRunRequest(
            job_id=job.id,
            task_id=job.task_id,
            target_url=first_step.target_url,
            fields=first_step.fields,
            field_definitions=first_step.field_definitions,
            submission=first_step.submission,
            entry_action=first_step.entry_action,
            workflow_steps=protected_steps if payload.workflow_steps else [],
        )
        with self._lock:
            self._jobs[job.id] = job
            self._requests[job.id] = request
            self._subscribers[job.id] = set()
        if self._repository is not None:
            self._repository.save(job)
        return job

    def _protect_step(self, task_id: str, step: WorkflowStep) -> WorkflowStep:
        protected: dict[str, str] = {}
        definitions = {definition.key: definition for definition in step.field_definitions}
        for canonical, value in step.fields.items():
            definition = definitions[canonical]
            if definition.source_field is not None:
                continue
            if definition.sensitive:
                protected[canonical] = self._secret_store.put(
                    f"{task_id}/{step.id}/{canonical}", value
                )
            else:
                protected[canonical] = value
        fields, definitions_list = resolve_field_definitions(protected, step.field_definitions)
        return step.model_copy(update={"fields": fields, "field_definitions": definitions_list})

    @staticmethod
    def _configuration_snapshot(steps: list[WorkflowStep]) -> dict[str, Any]:
        return {
            "version": 1,
            "steps": [
                {
                    "id": step.id,
                    "name": step.name,
                    "target_url": BrowserJobManager._safe_display_url(step.target_url),
                    "field_names": list(step.fields),
                    "field_definitions": [
                        definition.model_dump(mode="json")
                        for definition in step.field_definitions
                    ],
                    "entry_action": step.entry_action.model_dump(mode="json"),
                    "submission": step.submission.model_dump(mode="json"),
                }
                for step in steps
            ],
        }

    @staticmethod
    def _safe_display_url(value: str) -> str:
        parsed = urlsplit(value)
        hostname = parsed.hostname or ""
        host = f"[{hostname}]" if ":" in hostname else hostname
        port = f":{parsed.port}" if parsed.port else ""
        return urlunsplit((parsed.scheme, f"{host}{port}", parsed.path, "", ""))

    def start(self, job_id: str) -> None:
        """Schedule a job without delaying the HTTP response that created it."""

        self.get(job_id)
        self._track(job_id, self.run(job_id))

    async def resolve(self, job_id: str, resolution: HumanResolution) -> BrowserJob:
        job = self.get(job_id)
        if job.status is not BrowserJobStatus.NEED_HUMAN or job.intervention is None:
            raise ValueError("Browser job is not waiting for human confirmation")
        self._validate_resolution(job.intervention, resolution)
        await self._update(
            job_id,
            JobProgress(
                status=BrowserJobStatus.RESUMING,
                message="人工确认已接收, 正在重新扫描页面",
                current_url=job.current_url,
                completed_fields=job.completed_fields,
            ),
        )
        self._track(job_id, self._resume(job_id, resolution))
        return self.get(job_id)

    async def wait(self, job_id: str) -> None:
        task = self._running.get(job_id)
        if task is not None:
            await asyncio.shield(task)

    async def cancel_job(self, job_id: str) -> BrowserJob:
        job = self.get(job_id)
        terminal = {
            BrowserJobStatus.COMPLETED,
            BrowserJobStatus.FAILED,
            BrowserJobStatus.CANCELLED,
        }
        if job.status in terminal:
            raise ValueError("Terminal browser job cannot be cancelled")
        running = self._running.pop(job_id, None)
        current = asyncio.current_task()
        if running is not None and running is not current and not running.done():
            running.cancel()
            with suppress(asyncio.CancelledError):
                await running
        await self._worker.cancel(job_id)
        await self._update(
            job_id,
            JobProgress(
                status=BrowserJobStatus.CANCELLED,
                message="任务已由操作员终止, 浏览器会话已释放",
                current_url=job.current_url,
                completed_fields=job.completed_fields,
            ),
        )
        return self.get(job_id)

    def get(self, job_id: str) -> BrowserJob:
        with self._lock:
            try:
                return self._jobs[job_id]
            except KeyError as error:
                raise BrowserJobNotFoundError(job_id) from error

    def list(self) -> list[BrowserJob]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda job: job.created_at, reverse=True)

    def get_screenshot_path(self, job_id: str) -> str:
        self.get(job_id)
        with self._lock:
            try:
                return self._screenshot_paths[job_id]
            except KeyError as error:
                raise FileNotFoundError(job_id) from error

    def subscribe(self, job_id: str) -> asyncio.Queue[BrowserJob]:
        job = self.get(job_id)
        queue: asyncio.Queue[BrowserJob] = asyncio.Queue(maxsize=25)
        queue.put_nowait(job)
        with self._lock:
            self._subscribers[job_id].add(queue)
        return queue

    def unsubscribe(self, job_id: str, queue: asyncio.Queue[BrowserJob]) -> None:
        with self._lock:
            subscribers = self._subscribers.get(job_id)
            if subscribers is not None:
                subscribers.discard(queue)

    async def run(self, job_id: str) -> None:
        request = self._get_request(job_id)
        await self._update(
            job_id,
            JobProgress(status=BrowserJobStatus.STARTING, message="正在启动隔离浏览器"),
        )
        try:
            result = await self._worker.run(
                request,
                lambda progress: self._update(job_id, progress),
            )
        except Exception:
            await self._update(
                job_id,
                JobProgress(
                    status=BrowserJobStatus.FAILED,
                    message="Browser Worker 执行失败, 请查看服务端诊断日志",
                ),
            )
            return
        await self._update(job_id, result)

    async def _resume(self, job_id: str, resolution: HumanResolution) -> None:
        try:
            result = await self._worker.resume(
                job_id,
                resolution,
                lambda progress: self._update(job_id, progress),
            )
        except Exception:
            await self._update(
                job_id,
                JobProgress(
                    status=BrowserJobStatus.FAILED,
                    message="Browser Worker 恢复失败, 请查看服务端诊断日志",
                ),
            )
            return
        await self._update(job_id, result)

    def _track(self, job_id: str, coroutine: Coroutine[Any, Any, None]) -> None:
        task: asyncio.Task[None] = asyncio.create_task(
            coroutine,
            name=f"browser-job-{job_id}",
        )
        self._running[job_id] = task

        def forget(completed: asyncio.Task[None]) -> None:
            if self._running.get(job_id) is completed:
                self._running.pop(job_id, None)

        task.add_done_callback(forget)

    @staticmethod
    def _validate_resolution(
        intervention: HumanIntervention,
        resolution: HumanResolution,
    ) -> None:
        if intervention.kind is InterventionKind.SUBMISSION_CONFIRMATION:
            if not resolution.approve_submission:
                raise ValueError("Submission requires explicit approval")
            allowed_submit_ids = {
                candidate.element_id for candidate in intervention.submission_candidates
            }
            if resolution.submit_element_id not in allowed_submit_ids:
                raise ValueError(
                    "Submission must reference a current intervention candidate"
                )
            if resolution.field_mappings:
                raise ValueError("Submission confirmation cannot include field mappings")
            return

        if intervention.kind is InterventionKind.ENTRY_ACTION_CONFIRMATION:
            if not resolution.approve_entry_action:
                raise ValueError("Entry action requires explicit approval")
            allowed_entry_ids = {
                candidate.element_id for candidate in intervention.entry_candidates
            }
            if resolution.entry_element_id not in allowed_entry_ids:
                raise ValueError("Entry action must reference a current candidate")
            if resolution.field_mappings:
                raise ValueError("Entry action confirmation cannot include field mappings")
            return

        allowed = {
            candidate_set.canonical_field: {
                candidate.element_id for candidate in candidate_set.candidates
            }
            for candidate_set in intervention.field_candidates
        }
        for canonical, element_id in resolution.field_mappings.items():
            if element_id not in allowed.get(canonical, set()):
                raise ValueError("Human mapping must reference a current intervention candidate")
        required = {
            candidate_set.canonical_field
            for candidate_set in intervention.field_candidates
            if candidate_set.candidates
        }
        if required - resolution.field_mappings.keys():
            raise ValueError("Every unresolved field requires a selected candidate")

    def _get_request(self, job_id: str) -> BrowserRunRequest:
        self.get(job_id)
        with self._lock:
            return self._requests[job_id]

    async def _update(self, job_id: str, progress: JobProgress) -> None:
        with self._lock:
            current = self._jobs[job_id]
            event = JobEvent(
                sequence=len(current.events) + 1,
                status=progress.status,
                message=progress.message,
                field=progress.current_field,
                step_index=progress.current_step or current.current_step,
                step_name=progress.current_step_name or current.current_step_name,
            )
            updated = current.model_copy(
                update={
                    "status": progress.status,
                    "message": progress.message,
                    "current_url": (
                        self._safe_display_url(progress.current_url)
                        if progress.current_url
                        else current.current_url
                    ),
                    "current_field": progress.current_field,
                    "completed_fields": progress.completed_fields,
                    "submitted": current.submitted or progress.submitted,
                    "entry_action_performed": (
                        current.entry_action_performed
                        or progress.entry_action_performed
                    ),
                    "current_step": progress.current_step or current.current_step,
                    "current_step_name": (
                        progress.current_step_name or current.current_step_name
                    ),
                    "screenshot_url": (
                        f"/api/v1/browser/jobs/{job_id}/screenshot"
                        if progress.screenshot_path
                        else current.screenshot_url
                    ),
                    "intervention": progress.intervention,
                    "events": [*current.events, event],
                    "updated_at": datetime.now(UTC),
                }
            )
            self._jobs[job_id] = updated
            if progress.screenshot_path:
                self._screenshot_paths[job_id] = progress.screenshot_path
            subscribers = list(self._subscribers[job_id])
        if self._repository is not None:
            self._repository.save(updated)
        for queue in subscribers:
            if queue.full():
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            queue.put_nowait(updated)
