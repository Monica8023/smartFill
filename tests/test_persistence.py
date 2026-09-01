from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from smartfill.batch_jobs import (
    BatchItem,
    BatchItemStatus,
    BatchRun,
    BatchStatus,
)
from smartfill.browser_jobs import BrowserJob, BrowserJobStatus, JobEvent
from smartfill.domain import Task
from smartfill.persistence import (
    SqlAlchemyBatchRepository,
    SqlAlchemyBrowserJobRepository,
    SqlAlchemySystemSettingsRepository,
    SqlAlchemyTaskRepository,
    create_schema,
)


def make_engine():
    return create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def test_sql_repositories_persist_tasks_job_snapshots_and_timeline(tmp_path: Path) -> None:
    engine = make_engine()
    create_schema(engine)
    tasks = SqlAlchemyTaskRepository(engine)
    jobs = SqlAlchemyBrowserJobRepository(engine)

    task = tasks.save(
        Task(name="登录后完善资料", target_origin="https://target.example.com", record_count=1)
    )
    job = BrowserJob(
        task_id=task.id,
        name=task.name,
        target_url="https://target.example.com/login",
        field_names=["account.username", "account.password", "person.fullName"],
        total_fields=3,
        total_steps=2,
        current_step=1,
        current_step_name="登录",
        configuration_snapshot={
            "steps": [
                {"name": "登录", "field_names": ["account.username", "account.password"]},
                {"name": "完善资料", "field_names": ["person.fullName"]},
            ]
        },
        events=[
            JobEvent(
                sequence=1,
                status=BrowserJobStatus.QUEUED,
                message="任务已进入队列",
                step_index=1,
                step_name="登录",
            )
        ],
    )
    jobs.save(job)
    jobs.save(
        job.model_copy(
            update={
                "status": BrowserJobStatus.FILLING,
                "events": [
                    *job.events,
                    JobEvent(
                        sequence=2,
                        status=BrowserJobStatus.FILLING,
                        message="正在填写登录名",
                        field="account.username",
                        step_index=1,
                        step_name="登录",
                    ),
                ],
            }
        )
    )

    restored_task = tasks.get(task.id)
    restored_job = jobs.get(job.id)
    assert restored_task.name == "登录后完善资料"
    assert restored_job.configuration_snapshot["steps"][1]["name"] == "完善资料"
    assert [event.sequence for event in restored_job.events] == [1, 2]
    assert jobs.list()[0].id == job.id


def test_system_settings_store_replaces_origins_atomically() -> None:
    engine = make_engine()
    create_schema(engine)
    settings = SqlAlchemySystemSettingsRepository(engine)

    assert settings.get_target_origins() is None
    first = settings.replace_target_origins(["https://a.example.com"])
    second = settings.replace_target_origins(
        ["https://b.example.com", "http://127.0.0.1:8000"]
    )

    assert first.origins == ["https://a.example.com"]
    assert second.origins == ["https://b.example.com", "http://127.0.0.1:8000"]
    assert settings.get_target_origins() == second


def test_batch_repository_persists_progress_without_imported_values() -> None:
    engine = make_engine()
    create_schema(engine)
    tasks = SqlAlchemyTaskRepository(engine)
    jobs = SqlAlchemyBrowserJobRepository(engine)
    batches = SqlAlchemyBatchRepository(engine)
    task = tasks.save(
        Task(name="批量完善资料", target_origin="https://target.example.com", record_count=2)
    )
    template = jobs.save(
        BrowserJob(
            task_id=task.id,
            name="资料工作流",
            target_url="https://target.example.com/profile",
            field_names=["person.fullName"],
            total_fields=1,
            configuration_snapshot={"steps": []},
        )
    )
    batch = BatchRun(
        task_id=task.id,
        workflow_job_id=template.id,
        workflow_name=template.name,
        name="九月导入",
        source_filename="people.csv",
        status=BatchStatus.RUNNING,
        total_records=2,
        completed_records=1,
        mapping_snapshot={"name": "person.fullName"},
        items=[
            BatchItem(row_number=2, status=BatchItemStatus.COMPLETED),
            BatchItem(row_number=3),
        ],
    )

    batches.save(batch)
    restored = batches.get(batch.id)

    assert restored.mapping_snapshot == {"name": "person.fullName"}
    assert [item.row_number for item in restored.items] == [2, 3]
    assert "Alice" not in restored.model_dump_json()
    assert batches.list()[0].id == batch.id
