"""Browser automation job models, storage, progress streaming, and secret handling."""

from __future__ import annotations

import asyncio
import logging
import traceback
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import suppress
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from smartfill.config import normalize_origin
from smartfill.field_schema import (
    FIELD_KEY_PATTERN,
    FieldDefinition,
    FieldInputKind,
    resolve_field_definitions,
)
from smartfill.secrets import SecretStore

logger = logging.getLogger(__name__)


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
    AUTO = "auto"
    DIRECT = "direct"
    CLICK = "click"


class AuthenticationMode(StrEnum):
    NONE = "none"
    LOGIN = "login"
    REGISTER = "register"
    MANUAL = "manual"


def _validate_authentication_session(
    *,
    target_url: str,
    authentication_mode: AuthenticationMode,
    session_key: str | None,
    heartbeat_url: str | None,
) -> None:
    if (session_key is None) != (heartbeat_url is None):
        raise ValueError("Authentication session key and heartbeat URL must be configured together")
    if authentication_mode is AuthenticationMode.MANUAL and (
        session_key is None or heartbeat_url is None
    ):
        raise ValueError("Manual login requires a session key and heartbeat URL")
    if session_key is not None and (
        not session_key
        or len(session_key) > 100
        or any(not (char.isalnum() or char in "._:-") for char in session_key)
    ):
        raise ValueError("Invalid authentication session key")
    if heartbeat_url is None:
        return
    heartbeat = urlsplit(heartbeat_url)
    if (
        heartbeat.scheme not in {"http", "https"}
        or not heartbeat.hostname
        or heartbeat.username
        or heartbeat.password
    ):
        raise ValueError("A valid HTTP(S) heartbeat URL is required")
    if heartbeat.query or heartbeat.fragment:
        raise ValueError("Heartbeat URL cannot contain a query or fragment")
    if normalize_origin(target_url) != normalize_origin(heartbeat_url):
        raise ValueError("Heartbeat URL must use the same origin as the target URL")


class EntryActionConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    mode: EntryActionMode = EntryActionMode.AUTO
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
    fields: dict[str, str] = Field(default_factory=dict, max_length=30)
    field_definitions: list[FieldDefinition] = Field(default_factory=list, max_length=30)
    submission: SubmissionConfig = Field(default_factory=SubmissionConfig)
    entry_action: EntryActionConfig = Field(default_factory=EntryActionConfig)
    target_intent: str = Field(default="", max_length=500)
    authentication_mode: AuthenticationMode = AuthenticationMode.NONE
    observation_interval_seconds: float = Field(default=5, ge=1, le=30)
    authentication_session_key: str | None = Field(default=None, max_length=100)
    heartbeat_url: str | None = Field(default=None, max_length=2_048)
    heartbeat_interval_seconds: int = Field(default=300, ge=30, le=3_600)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(char) < 32 for char in value):
            raise ValueError("Workflow step name cannot be blank")
        return value

    @field_validator("target_intent")
    @classmethod
    def normalize_target_intent(cls, value: str) -> str:
        return " ".join(value.split())

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
        _validate_authentication_session(
            target_url=self.target_url,
            authentication_mode=self.authentication_mode,
            session_key=self.authentication_session_key,
            heartbeat_url=self.heartbeat_url,
        )
        if not self.fields and not self.target_intent:
            raise ValueError("A workflow step requires fields or a target intent")
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
    keep_browser_open: bool = False
    target_intent: str = Field(default="", max_length=500)
    authentication_mode: AuthenticationMode = AuthenticationMode.NONE
    observation_interval_seconds: float = Field(default=5, ge=1, le=30)
    authentication_session_key: str | None = Field(default=None, max_length=100)
    heartbeat_url: str | None = Field(default=None, max_length=2_048)
    heartbeat_interval_seconds: int = Field(default=300, ge=30, le=3_600)

    @field_validator("target_intent")
    @classmethod
    def normalize_target_intent(cls, value: str) -> str:
        return " ".join(value.split())

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
        _validate_authentication_session(
            target_url=self.target_url,
            authentication_mode=self.authentication_mode,
            session_key=self.authentication_session_key,
            heartbeat_url=self.heartbeat_url,
        )
        if not self.fields and not self.target_intent:
            raise ValueError("At least one field, workflow step, or target intent is required")
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
    keep_browser_open: bool = False
    target_intent: str = Field(default="", max_length=500)
    authentication_mode: AuthenticationMode = AuthenticationMode.NONE
    observation_interval_seconds: float = Field(default=5, ge=1, le=30)
    authentication_session_key: str | None = Field(default=None, max_length=100)
    heartbeat_url: str | None = Field(default=None, max_length=2_048)
    heartbeat_interval_seconds: int = Field(default=300, ge=30, le=3_600)

    @model_validator(mode="after")
    def prepare_dynamic_fields(self) -> BrowserRunRequest:
        _validate_authentication_session(
            target_url=self.target_url,
            authentication_mode=self.authentication_mode,
            session_key=self.authentication_session_key,
            heartbeat_url=self.heartbeat_url,
        )
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
    VISUAL_REVIEW = "visual_review"
    HUMAN_CHALLENGE = "human_challenge"
    VERIFICATION = "verification"
    SUBMISSION_CONFIRMATION = "submission_confirmation"
    ENTRY_ACTION_CONFIRMATION = "entry_action_confirmation"
    DATA_REQUIRED = "data_required"
    MANUAL_LOGIN = "manual_login"


class RequiredDataField(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    display_name: str = Field(min_length=1, max_length=120)
    input_kind: FieldInputKind = FieldInputKind.TEXT
    sensitive: bool = False
    reason: str = Field(default="目标表单必填", max_length=300)

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        if FIELD_KEY_PATTERN.fullmatch(value) is None:
            raise ValueError(f"Invalid field key: {value}")
        return value


class InterventionCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    element_id: str = Field(min_length=1, max_length=200)
    accessible_name: str = Field(default="", max_length=300)
    role: str = Field(default="", max_length=80)
    tag: str = Field(default="", max_length=40)
    frame_path: str = Field(default="main", max_length=300)
    confidence: float = Field(default=0, ge=0, le=1)


class HumanIntervention(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: InterventionKind
    instruction: str = Field(min_length=1, max_length=500)
    submission_candidates: list[InterventionCandidate] = Field(
        default_factory=list,
        max_length=20,
    )
    entry_candidates: list[InterventionCandidate] = Field(
        default_factory=list,
        max_length=20,
    )
    requires_browser_interaction: bool = False
    missing_fields: list[RequiredDataField] = Field(default_factory=list, max_length=30)


class HumanResolution(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    approve_submission: bool = False
    submit_element_id: str | None = Field(default=None, max_length=200)
    approve_entry_action: bool = False
    entry_element_id: str | None = Field(default=None, max_length=200)
    field_values: dict[str, str] = Field(default_factory=dict, max_length=30)
    manual_login_completed: bool = False

    @field_validator("field_values")
    @classmethod
    def validate_field_values(cls, values: dict[str, str]) -> dict[str, str]:
        for key, value in values.items():
            if FIELD_KEY_PATTERN.fullmatch(key) is None:
                raise ValueError(f"Invalid field key: {key}")
            if not value or len(value) > 4_096:
                raise ValueError(f"Invalid value for field: {key}")
        return values


class ExecutionStatistics(BaseModel):
    """Cumulative, non-sensitive measurements for one browser execution."""

    model_config = ConfigDict(frozen=True)

    duration_ms: int = Field(default=0, ge=0)
    screenshot_count: int = Field(default=0, ge=0)
    model_call_count: int = Field(default=0, ge=0)
    model_latency_ms: int = Field(default=0, ge=0)
    browser_action_count: int = Field(default=0, ge=0)
    click_count: int = Field(default=0, ge=0)
    scroll_count: int = Field(default=0, ge=0)
    wait_count: int = Field(default=0, ge=0)
    fill_count: int = Field(default=0, ge=0)

    def summary(self) -> str:
        return (
            f"执行统计: {self.duration_ms / 1_000:.2f} 秒, "
            f"截图 {self.screenshot_count} 次, 模型调用 {self.model_call_count} 次, "
            f"浏览器动作 {self.browser_action_count} 次"
        )


class JobProgress(BaseModel):
    status: BrowserJobStatus
    message: str = Field(max_length=500)
    current_url: str | None = None
    current_field: str | None = None
    completed_fields: int = Field(default=0, ge=0)
    screenshot_path: str | None = None
    download_paths: list[str] = Field(default_factory=list, max_length=20)
    intervention: HumanIntervention | None = None
    submitted: bool = False
    entry_action_performed: bool = False
    current_step: int | None = Field(default=None, ge=1)
    current_step_name: str | None = Field(default=None, max_length=120)
    diagnostic_id: str | None = Field(default=None, max_length=64)
    diagnostic_url: str | None = Field(default=None, max_length=2_048)
    browser_session_open: bool | None = None
    statistics: ExecutionStatistics | None = None


class BrowserRunResult(JobProgress):
    pass


class BrowserAutomationWorker(Protocol):
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
    entry_action_mode: EntryActionMode = EntryActionMode.AUTO
    entry_action_performed: bool = False
    current_step: int = Field(default=1, ge=1)
    current_step_name: str = "表单填写"
    total_steps: int = Field(default=1, ge=1, le=10)
    configuration_snapshot: dict[str, Any] = Field(default_factory=dict)
    screenshot_url: str | None = None
    download_urls: list[str] = Field(default_factory=list, max_length=20)
    intervention: HumanIntervention | None = None
    diagnostic_id: str | None = Field(default=None, max_length=64)
    diagnostic_url: str | None = Field(default=None, max_length=2_048)
    statistics: ExecutionStatistics | None = None
    statistics_url: str | None = Field(default=None, max_length=2_048)
    browser_session_open: bool = False
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
        artifacts_root: Path = Path("artifacts"),
        terminal_status_callback: Callable[[str, BrowserJobStatus], None] | None = None,
    ) -> None:
        self._worker = worker
        self._secret_store = secret_store
        self._repository = repository
        self._artifacts_root = artifacts_root.resolve()
        self._terminal_status_callback = terminal_status_callback
        restored = repository.list() if repository is not None else []
        # A browser process cannot survive a service restart even if the last
        # persisted job snapshot said its inspection session was still open.
        restored = [
            job.model_copy(update={"browser_session_open": False})
            for job in restored
        ]
        self._jobs: dict[str, BrowserJob] = {job.id: job for job in restored}
        self._requests: dict[str, BrowserRunRequest] = {}
        self._screenshot_paths: dict[str, str] = {}
        self._download_paths: dict[str, list[str]] = {}
        self._subscribers: dict[str, set[asyncio.Queue[BrowserJob]]] = {
            job.id: set() for job in restored
        }
        self._running: dict[str, asyncio.Task[None]] = {}
        self._lock = RLock()

    def create_or_reuse_active(
        self,
        payload: BrowserJobCreate,
        *,
        task_name: str | None = None,
    ) -> tuple[BrowserJob, bool]:
        """Create a job unless the same login session already has active work."""

        source_step = payload.workflow_steps[0] if payload.workflow_steps else payload
        session_key = source_step.authentication_session_key
        target_origin = normalize_origin(source_step.target_url)
        terminal = {
            BrowserJobStatus.COMPLETED,
            BrowserJobStatus.FAILED,
            BrowserJobStatus.CANCELLED,
        }
        with self._lock:
            if session_key is not None:
                for existing in sorted(
                    self._jobs.values(),
                    key=lambda job: job.created_at,
                    reverse=True,
                ):
                    request = self._requests.get(existing.id)
                    if (
                        request is not None
                        and existing.status not in terminal
                        and request.authentication_session_key == session_key
                        and normalize_origin(request.target_url) == target_origin
                    ):
                        return existing, False
            return self.create(payload, task_name=task_name), True

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
                target_intent=payload.target_intent,
                authentication_mode=payload.authentication_mode,
                observation_interval_seconds=payload.observation_interval_seconds,
                authentication_session_key=payload.authentication_session_key,
                heartbeat_url=payload.heartbeat_url,
                heartbeat_interval_seconds=payload.heartbeat_interval_seconds,
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
            target_intent=first_step.target_intent,
            authentication_mode=first_step.authentication_mode,
            observation_interval_seconds=first_step.observation_interval_seconds,
            authentication_session_key=first_step.authentication_session_key,
            heartbeat_url=first_step.heartbeat_url,
            heartbeat_interval_seconds=first_step.heartbeat_interval_seconds,
            workflow_steps=protected_steps if payload.workflow_steps else [],
            keep_browser_open=payload.keep_browser_open,
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
                    "field_values": {
                        definition.key: step.fields[definition.key]
                        for definition in step.field_definitions
                        if (
                            definition.source_field is None
                            and not definition.sensitive
                            and definition.key in step.fields
                        )
                    },
                    "field_definitions": [
                        definition.model_dump(mode="json")
                        for definition in step.field_definitions
                    ],
                    "entry_action": step.entry_action.model_dump(mode="json"),
                    "submission": step.submission.model_dump(mode="json"),
                    "target_intent": step.target_intent,
                    "authentication_mode": step.authentication_mode.value,
                    "observation_interval_seconds": step.observation_interval_seconds,
                    "authentication_session_key": step.authentication_session_key,
                    "heartbeat_url": step.heartbeat_url,
                    "heartbeat_interval_seconds": step.heartbeat_interval_seconds,
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
        protected_resolution = self._protect_resolution_values(
            job.task_id,
            job.intervention,
            resolution,
        )
        await self._update(
            job_id,
            JobProgress(
                status=BrowserJobStatus.RESUMING,
                message="人工确认已接收, 正在重新扫描页面",
                current_url=job.current_url,
                completed_fields=job.completed_fields,
            ),
        )
        self._track(job_id, self._resume(job_id, protected_resolution))
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

    async def close_browser(self, job_id: str) -> BrowserJob:
        """Release a completed job's browser retained for operator inspection."""

        job = self.get(job_id)
        if job.status is not BrowserJobStatus.COMPLETED:
            raise ValueError("Only a completed browser job can close its browser")
        if not job.browser_session_open:
            return job
        await self._worker.cancel(job_id)
        await self._update(
            job_id,
            JobProgress(
                status=BrowserJobStatus.COMPLETED,
                message="工作流执行完成, 浏览器已由操作员关闭",
                current_url=job.current_url,
                completed_fields=job.completed_fields,
                current_step=job.current_step,
                current_step_name=job.current_step_name,
                browser_session_open=False,
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

    def get_diagnostic_path(self, job_id: str) -> str:
        job = self.get(job_id)
        if job.diagnostic_id is None:
            raise FileNotFoundError(job_id)
        path = self._diagnostic_path(job_id, job.diagnostic_id)
        if not path.is_file():
            raise FileNotFoundError(job_id)
        return str(path)

    def get_download_path(self, job_id: str, index: int) -> str:
        self.get(job_id)
        with self._lock:
            paths = self._download_paths.get(job_id)
            if paths is None:
                downloads_dir = self._artifacts_root / "jobs" / job_id / "downloads"
                paths = (
                    [
                        str(path.resolve())
                        for path in sorted(downloads_dir.iterdir())
                        if path.is_file()
                    ]
                    if downloads_dir.is_dir()
                    else []
                )
                self._download_paths[job_id] = paths
            if index < 0 or index >= len(paths):
                raise FileNotFoundError(job_id)
            path = Path(paths[index]).resolve()
            if not path.is_file() or not path.is_relative_to(self._artifacts_root):
                raise FileNotFoundError(job_id)
            return str(path)

    def get_statistics_path(self, job_id: str) -> str:
        job = self.get(job_id)
        if job.statistics is None or job.statistics_url is None:
            raise FileNotFoundError(job_id)
        path = self._statistics_path(job_id)
        if not path.is_file():
            raise FileNotFoundError(job_id)
        return str(path)

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
        except Exception as error:
            current = self.get(job_id)
            diagnostic_id, diagnostic_url = self._record_failure(
                job_id,
                phase="run",
                error=error,
            )
            await self._update(
                job_id,
                JobProgress(
                    status=BrowserJobStatus.FAILED,
                    message=(
                        "Browser Worker 执行失败; "
                        f"诊断 ID: {diagnostic_id}, 可下载诊断日志"
                    ),
                    diagnostic_id=diagnostic_id,
                    diagnostic_url=diagnostic_url,
                    current_url=current.current_url,
                    completed_fields=current.completed_fields,
                    current_step=current.current_step,
                    current_step_name=current.current_step_name,
                ),
            )
            self._notify_terminal_status(job_id, BrowserJobStatus.FAILED)
            return
        await self._update(job_id, result)
        self._notify_terminal_status(job_id, result.status)

    async def _resume(self, job_id: str, resolution: HumanResolution) -> None:
        try:
            result = await self._worker.resume(
                job_id,
                resolution,
                lambda progress: self._update(job_id, progress),
            )
        except Exception as error:
            current = self.get(job_id)
            diagnostic_id, diagnostic_url = self._record_failure(
                job_id,
                phase="resume",
                error=error,
            )
            await self._update(
                job_id,
                JobProgress(
                    status=BrowserJobStatus.FAILED,
                    message=(
                        "Browser Worker 恢复失败; "
                        f"诊断 ID: {diagnostic_id}, 可下载诊断日志"
                    ),
                    diagnostic_id=diagnostic_id,
                    diagnostic_url=diagnostic_url,
                    current_url=current.current_url,
                    completed_fields=current.completed_fields,
                    current_step=current.current_step,
                    current_step_name=current.current_step_name,
                ),
            )
            self._notify_terminal_status(job_id, BrowserJobStatus.FAILED)
            return
        await self._update(job_id, result)
        self._notify_terminal_status(job_id, result.status)

    def _record_failure(
        self,
        job_id: str,
        *,
        phase: str,
        error: Exception,
    ) -> tuple[str, str]:
        diagnostic_id = uuid4().hex[:12]
        job = self.get(job_id)
        path = self._diagnostic_path(job_id, diagnostic_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        details = self._redact_diagnostic_secrets(
            job_id,
            "".join(
                [
                    f"diagnostic_id: {diagnostic_id}\n",
                    f"job_id: {job_id}\n",
                    f"task_id: {job.task_id}\n",
                    f"phase: {phase}\n",
                    f"job_status: {job.status.value}\n",
                    f"current_step: {job.current_step} ({job.current_step_name})\n",
                    f"current_url: {job.current_url or 'unknown'}\n",
                    f"created_at: {datetime.now(UTC).isoformat()}\n\n",
                    *traceback.format_exception(type(error), error, error.__traceback__),
                ]
            ),
        )
        path.write_text(details, encoding="utf-8")
        logger.error(
            "Browser Worker failed diagnostic_id=%s job_id=%s phase=%s diagnostic=%s",
            diagnostic_id,
            job_id,
            phase,
            path,
        )
        return diagnostic_id, f"/api/v1/browser/jobs/{job_id}/diagnostic"

    def _redact_diagnostic_secrets(self, job_id: str, value: str) -> str:
        request = self._requests.get(job_id)
        if request is None:
            return value
        field_groups = (
            [step.fields for step in request.workflow_steps]
            if request.workflow_steps
            else [request.fields]
        )
        secrets: set[str] = set()
        for fields in field_groups:
            for field_value in fields.values():
                if not field_value.startswith("secret://"):
                    continue
                try:
                    secrets.add(self._secret_store.resolve(field_value))
                except (KeyError, ValueError):
                    continue
        redacted = value
        for secret in sorted(secrets, key=len, reverse=True):
            if secret:
                redacted = redacted.replace(secret, "[REDACTED]")
        return redacted

    def _diagnostic_path(self, job_id: str, diagnostic_id: str) -> Path:
        return (
            self._artifacts_root
            / "jobs"
            / job_id
            / f"diagnostic-{diagnostic_id}.log"
        )

    def _statistics_path(self, job_id: str) -> Path:
        return self._artifacts_root / "jobs" / job_id / "execution-statistics.log"

    def _write_statistics_log(self, job: BrowserJob) -> None:
        statistics = job.statistics
        if statistics is None:
            return
        path = self._statistics_path(job.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "SmartFill browser execution statistics",
            f"job_id: {job.id}",
            f"task_id: {job.task_id}",
            f"status: {job.status.value}",
            f"duration_ms: {statistics.duration_ms}",
            f"screenshot_count: {statistics.screenshot_count}",
            f"model_call_count: {statistics.model_call_count}",
            f"model_latency_ms: {statistics.model_latency_ms}",
            f"browser_action_count: {statistics.browser_action_count}",
            f"click_count: {statistics.click_count}",
            f"scroll_count: {statistics.scroll_count}",
            f"wait_count: {statistics.wait_count}",
            f"fill_count: {statistics.fill_count}",
            f"final_url: {job.current_url or 'unknown'}",
            f"completed_at: {job.updated_at.isoformat()}",
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logger.info(
            "Browser Worker execution statistics job_id=%s duration_ms=%s "
            "screenshots=%s model_calls=%s browser_actions=%s",
            job.id,
            statistics.duration_ms,
            statistics.screenshot_count,
            statistics.model_call_count,
            statistics.browser_action_count,
        )

    def _notify_terminal_status(
        self,
        job_id: str,
        status: BrowserJobStatus,
    ) -> None:
        if self._terminal_status_callback is None or status not in {
            BrowserJobStatus.COMPLETED,
            BrowserJobStatus.FAILED,
            BrowserJobStatus.CANCELLED,
        }:
            return
        try:
            self._terminal_status_callback(self.get(job_id).task_id, status)
        except Exception:
            logger.exception(
                "Browser task status synchronization failed job_id=%s status=%s",
                job_id,
                status.value,
            )

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
            return

        if intervention.kind is InterventionKind.ENTRY_ACTION_CONFIRMATION:
            if not resolution.approve_entry_action:
                raise ValueError("Entry action requires explicit approval")
            allowed_entry_ids = {
                candidate.element_id for candidate in intervention.entry_candidates
            }
            if resolution.entry_element_id not in allowed_entry_ids:
                raise ValueError("Entry action must reference a current candidate")
            return

        if intervention.kind is InterventionKind.DATA_REQUIRED:
            required = {field.key for field in intervention.missing_fields}
            supplied = set(resolution.field_values)
            if required != supplied:
                raise ValueError("Every missing field requires exactly one supplied value")
            return

        if intervention.kind is InterventionKind.MANUAL_LOGIN:
            if not resolution.manual_login_completed:
                raise ValueError("Manual login requires explicit completion confirmation")
            return

        if resolution.field_values:
            raise ValueError("Field values are accepted only for a missing-data intervention")

    def _protect_resolution_values(
        self,
        task_id: str,
        intervention: HumanIntervention,
        resolution: HumanResolution,
    ) -> HumanResolution:
        if intervention.kind is not InterventionKind.DATA_REQUIRED:
            return resolution
        requirements = {field.key: field for field in intervention.missing_fields}
        protected = {
            key: (
                self._secret_store.put(f"{task_id}/supplement/{key}", value)
                if requirements[key].sensitive
                else value
            )
            for key, value in resolution.field_values.items()
        }
        return resolution.model_copy(update={"field_values": protected})

    def _get_request(self, job_id: str) -> BrowserRunRequest:
        self.get(job_id)
        with self._lock:
            return self._requests[job_id]

    async def _update(self, job_id: str, progress: JobProgress) -> None:
        with self._lock:
            current = self._jobs[job_id]
            download_urls = current.download_urls
            if progress.download_paths:
                safe_paths = []
                for raw_path in progress.download_paths:
                    path = Path(raw_path).resolve()
                    if path.is_file() and path.is_relative_to(self._artifacts_root):
                        safe_paths.append(str(path))
                self._download_paths[job_id] = safe_paths
                download_urls = [
                    f"/api/v1/browser/jobs/{job_id}/downloads/{index}"
                    for index in range(len(safe_paths))
                ]
            statistics = progress.statistics or current.statistics
            statistics_url = current.statistics_url
            if (
                progress.status
                in {
                    BrowserJobStatus.COMPLETED,
                    BrowserJobStatus.FAILED,
                    BrowserJobStatus.CANCELLED,
                }
                and statistics is not None
            ):
                statistics_url = f"/api/v1/browser/jobs/{job_id}/statistics"
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
                    "completed_fields": max(
                        current.completed_fields,
                        progress.completed_fields,
                    ),
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
                    "download_urls": download_urls,
                    "intervention": progress.intervention,
                    "diagnostic_id": progress.diagnostic_id or current.diagnostic_id,
                    "diagnostic_url": progress.diagnostic_url or current.diagnostic_url,
                    "statistics": statistics,
                    "statistics_url": statistics_url,
                    "browser_session_open": (
                        progress.browser_session_open
                        if progress.browser_session_open is not None
                        else current.browser_session_open
                    ),
                    "events": [*current.events, event],
                    "updated_at": datetime.now(UTC),
                }
            )
            self._jobs[job_id] = updated
            if progress.screenshot_path:
                self._screenshot_paths[job_id] = progress.screenshot_path
            subscribers = list(self._subscribers[job_id])
        if updated.statistics is not None and updated.statistics_url is not None:
            self._write_statistics_log(updated)
        if self._repository is not None:
            self._repository.save(updated)
        for queue in subscribers:
            if queue.full():
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            queue.put_nowait(updated)
