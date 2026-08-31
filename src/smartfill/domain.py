"""Core task entities and state transitions."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class TaskStatus(StrEnum):
    """Lifecycle states for a batch task."""

    DRAFT = "draft"
    VALIDATING = "validating"
    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class InvalidTransitionError(ValueError):
    """Raised when a task attempts an unsupported state transition."""


_ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.DRAFT: frozenset({TaskStatus.VALIDATING, TaskStatus.CANCELLED}),
    TaskStatus.VALIDATING: frozenset({TaskStatus.READY, TaskStatus.FAILED, TaskStatus.CANCELLED}),
    TaskStatus.READY: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED}),
    TaskStatus.RUNNING: frozenset(
        {TaskStatus.PAUSED, TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
    ),
    TaskStatus.PAUSED: frozenset({TaskStatus.RUNNING, TaskStatus.FAILED, TaskStatus.CANCELLED}),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}


def transition_task(current: TaskStatus, target: TaskStatus) -> TaskStatus:
    """Validate and return a task's next status."""

    if target not in _ALLOWED_TRANSITIONS[current]:
        raise InvalidTransitionError(f"Cannot transition task from {current} to {target}")
    return target


class Task(BaseModel):
    """A batch automation task without embedded credentials."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    target_origin: str
    record_count: int = Field(ge=0)
    status: TaskStatus = TaskStatus.DRAFT
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def transitioned(self, target: TaskStatus) -> Task:
        """Create an updated immutable task after validating its transition."""

        return self.model_copy(
            update={"status": transition_task(self.status, target), "updated_at": datetime.now(UTC)}
        )
