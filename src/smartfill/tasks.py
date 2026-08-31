"""In-memory task service used by the first executable vertical slice."""

from __future__ import annotations

from threading import RLock

from smartfill.domain import Task, TaskStatus


class TaskNotFoundError(LookupError):
    """Raised when a task ID does not exist."""


class InMemoryTaskRepository:
    """Thread-safe development repository; replace with PostgreSQL in production."""

    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}
        self._lock = RLock()

    def save(self, task: Task) -> Task:
        with self._lock:
            self._tasks[task.id] = task
        return task

    def get(self, task_id: str) -> Task:
        with self._lock:
            try:
                return self._tasks[task_id]
            except KeyError as error:
                raise TaskNotFoundError(task_id) from error

    def list(self) -> list[Task]:
        with self._lock:
            return sorted(self._tasks.values(), key=lambda task: task.created_at, reverse=True)


class TaskService:
    def __init__(self, repository: InMemoryTaskRepository) -> None:
        self._repository = repository

    def create(self, *, name: str, target_origin: str, record_count: int) -> Task:
        return self._repository.save(
            Task(name=name, target_origin=target_origin, record_count=record_count)
        )

    def list(self) -> list[Task]:
        return self._repository.list()

    def get(self, task_id: str) -> Task:
        return self._repository.get(task_id)

    def validate(self, task_id: str) -> Task:
        task = self.get(task_id).transitioned(TaskStatus.VALIDATING)
        return self._repository.save(task.transitioned(TaskStatus.READY))

    def start(self, task_id: str) -> Task:
        return self._transition(task_id, TaskStatus.RUNNING)

    def pause(self, task_id: str) -> Task:
        return self._transition(task_id, TaskStatus.PAUSED)

    def resume(self, task_id: str) -> Task:
        return self._transition(task_id, TaskStatus.RUNNING)

    def _transition(self, task_id: str, target: TaskStatus) -> Task:
        return self._repository.save(self.get(task_id).transitioned(target))
