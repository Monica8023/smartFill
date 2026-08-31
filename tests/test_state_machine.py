import pytest

from smartfill.domain import InvalidTransitionError, TaskStatus, transition_task


def test_task_can_follow_the_happy_path_and_resume() -> None:
    status = TaskStatus.DRAFT

    for target in (
        TaskStatus.VALIDATING,
        TaskStatus.READY,
        TaskStatus.RUNNING,
        TaskStatus.PAUSED,
        TaskStatus.RUNNING,
        TaskStatus.COMPLETED,
    ):
        status = transition_task(status, target)

    assert status is TaskStatus.COMPLETED


def test_terminal_task_cannot_be_restarted() -> None:
    with pytest.raises(InvalidTransitionError, match=r"completed.*running"):
        transition_task(TaskStatus.COMPLETED, TaskStatus.RUNNING)


def test_ready_task_cannot_skip_directly_to_completed() -> None:
    with pytest.raises(InvalidTransitionError):
        transition_task(TaskStatus.READY, TaskStatus.COMPLETED)
