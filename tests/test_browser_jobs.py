from collections.abc import Awaitable, Callable

import pytest

from smartfill.browser_jobs import (
    BrowserJobCreate,
    BrowserJobManager,
    BrowserJobStatus,
    BrowserRunRequest,
    BrowserRunResult,
    EntryActionConfig,
    EntryActionMode,
    FieldCandidateSet,
    HumanIntervention,
    HumanResolution,
    InterventionCandidate,
    InterventionKind,
    JobProgress,
    SubmissionConfig,
    SubmissionPolicy,
    WorkflowStep,
)
from smartfill.field_schema import FieldDefinition, FieldInputKind
from smartfill.secrets import InMemorySecretStore


class RecordingWorker:
    def __init__(self) -> None:
        self.request: BrowserRunRequest | None = None
        self.cancelled_job_id: str | None = None

    async def run(
        self,
        request: BrowserRunRequest,
        report: Callable[[JobProgress], Awaitable[None]],
    ) -> BrowserRunResult:
        self.request = request
        await report(
            JobProgress(
                status=BrowserJobStatus.FILLING,
                message="正在填写 person.fullName",
                current_url=request.target_url,
                completed_fields=0,
            )
        )
        return BrowserRunResult(
            status=BrowserJobStatus.COMPLETED,
            message="填写和验证完成, 未自动提交",
            current_url=request.target_url,
            completed_fields=len(request.fields),
        )

    async def resume(
        self,
        job_id: str,
        resolution: HumanResolution,
        report: Callable[[JobProgress], Awaitable[None]],
    ) -> BrowserRunResult:
        return BrowserRunResult(
            status=BrowserJobStatus.COMPLETED,
            message="人工确认后完成",
            completed_fields=1,
        )

    async def cancel(self, job_id: str) -> None:
        self.cancelled_job_id = job_id


class HumanWorker(RecordingWorker):
    async def run(
        self,
        request: BrowserRunRequest,
        report: Callable[[JobProgress], Awaitable[None]],
    ) -> BrowserRunResult:
        self.request = request
        return BrowserRunResult(
            status=BrowserJobStatus.NEED_HUMAN,
            message="字段需要确认",
            intervention=HumanIntervention(
                kind=InterventionKind.FIELD_MAPPING,
                instruction="选择姓名字段",
                field_candidates=[
                    FieldCandidateSet(
                        canonical_field="person.fullName",
                        candidates=[
                            InterventionCandidate(
                                element_id="sf-job-0",
                                accessible_name="姓名",
                                role="textbox",
                                tag="input",
                                frame_path="main",
                            )
                        ],
                    )
                ],
            ),
        )


class SubmissionConfirmationWorker(RecordingWorker):
    async def run(
        self,
        request: BrowserRunRequest,
        report: Callable[[JobProgress], Awaitable[None]],
    ) -> BrowserRunResult:
        self.request = request
        return BrowserRunResult(
            status=BrowserJobStatus.NEED_HUMAN,
            message="等待提交确认",
            completed_fields=1,
            intervention=HumanIntervention(
                kind=InterventionKind.SUBMISSION_CONFIRMATION,
                instruction="确认 Register 按钮",
                submission_candidates=[
                    InterventionCandidate(
                        element_id="sf-submit-job-0",
                        accessible_name="Register",
                        role="button",
                        tag="button",
                        frame_path="main",
                        confidence=1,
                    )
                ],
            ),
        )

    async def resume(
        self,
        job_id: str,
        resolution: HumanResolution,
        report: Callable[[JobProgress], Awaitable[None]],
    ) -> BrowserRunResult:
        return BrowserRunResult(
            status=BrowserJobStatus.COMPLETED,
            message="已提交",
            completed_fields=1,
            submitted=True,
        )


class EntryConfirmationWorker(RecordingWorker):
    async def run(
        self,
        request: BrowserRunRequest,
        report: Callable[[JobProgress], Awaitable[None]],
    ) -> BrowserRunResult:
        self.request = request
        return BrowserRunResult(
            status=BrowserJobStatus.NEED_HUMAN,
            message="等待登录入口确认",
            intervention=HumanIntervention(
                kind=InterventionKind.ENTRY_ACTION_CONFIRMATION,
                instruction="选择 Login 入口",
                entry_candidates=[
                    InterventionCandidate(
                        element_id="sf-entry-job-0",
                        accessible_name="Login",
                        role="link",
                        tag="a",
                        frame_path="main",
                        confidence=0.92,
                    )
                ],
            ),
        )

    async def resume(
        self,
        job_id: str,
        resolution: HumanResolution,
        report: Callable[[JobProgress], Awaitable[None]],
    ) -> BrowserRunResult:
        return BrowserRunResult(
            status=BrowserJobStatus.COMPLETED,
            message="进入登录页后填写完成",
            completed_fields=1,
            entry_action_performed=True,
        )

@pytest.mark.asyncio
async def test_job_manager_replaces_sensitive_values_before_worker_execution() -> None:
    worker = RecordingWorker()
    manager = BrowserJobManager(worker=worker, secret_store=InMemorySecretStore())

    job = manager.create(
        BrowserJobCreate(
            task_id="task-1",
            target_url="https://target.example.com/profile",
            fields={
                "person.fullName": "张三",
                "account.password": "P@ssw0rd!",
                "person.idNumber": "110101199001011234",
            },
        )
    )
    await manager.run(job.id)

    assert worker.request is not None
    assert worker.request.fields["person.fullName"] == "张三"
    assert worker.request.fields["account.password"].startswith("secret://")
    assert worker.request.fields["person.idNumber"].startswith("secret://")
    serialized = manager.get(job.id).model_dump_json()
    assert "P@ssw0rd!" not in serialized
    assert "110101199001011234" not in serialized


@pytest.mark.asyncio
async def test_job_accepts_dynamic_fields_and_protects_schema_marked_secrets() -> None:
    worker = RecordingWorker()
    manager = BrowserJobManager(worker=worker, secret_store=InMemorySecretStore())
    payload = BrowserJobCreate(
        task_id="task-dynamic",
        target_url="https://target.example.com/register",
        fields={
            "person.firstName": "San",
            "person.lastName": "Zhang",
            "person.ssn": "123-45-6789",
            "account.password": "P@ssw0rd!",
        },
        field_definitions=[
            FieldDefinition(
                key="person.firstName",
                display_name="名",
                aliases=["First Name", "Given Name"],
            ),
            FieldDefinition(
                key="person.lastName",
                display_name="姓",
                aliases=["Last Name", "Family Name"],
            ),
            FieldDefinition(
                key="person.ssn",
                display_name="SSN",
                aliases=["SSN"],
                sensitive=True,
            ),
            FieldDefinition(
                key="account.password",
                display_name="密码",
                aliases=["Password"],
                input_kind=FieldInputKind.PASSWORD,
                sensitive=True,
            ),
            FieldDefinition(
                key="account.passwordConfirmation",
                display_name="确认密码",
                aliases=["Confirm", "Confirm Password"],
                input_kind=FieldInputKind.PASSWORD,
                sensitive=True,
                source_field="account.password",
            ),
        ],
    )

    job = manager.create(payload)
    await manager.run(job.id)

    assert worker.request is not None
    assert worker.request.fields["person.firstName"] == "San"
    assert worker.request.fields["person.lastName"] == "Zhang"
    assert worker.request.fields["person.ssn"].startswith("secret://")
    assert worker.request.fields["account.password"].startswith("secret://")
    assert (
        worker.request.fields["account.passwordConfirmation"]
        == worker.request.fields["account.password"]
    )
    assert worker.request.field_definitions[-1].source_field == "account.password"
    assert job.total_fields == 5
    assert "123-45-6789" not in manager.get(job.id).model_dump_json()


def test_dynamic_field_schema_rejects_mismatched_and_unsafe_definitions() -> None:
    with pytest.raises(ValueError, match="definition"):
        BrowserJobCreate(
            task_id="task-1",
            target_url="https://target.example.com/register",
            fields={"person.firstName": "San"},
            field_definitions=[
                FieldDefinition(
                    key="person.lastName",
                    display_name="姓",
                    aliases=["Last Name"],
                )
            ],
        )

    with pytest.raises(ValueError):
        FieldDefinition(
            key="system.prompt\nignore",
            display_name="Unsafe",
            aliases=["First Name"],
        )

    with pytest.raises(ValueError, match="Password fields must be sensitive"):
        FieldDefinition(
            key="account.temporaryPassword",
            display_name="临时密码",
            aliases=["Temporary Password"],
            input_kind=FieldInputKind.PASSWORD,
            sensitive=False,
        )

    with pytest.raises(ValueError, match="Derived fields cannot also submit a value"):
        BrowserJobCreate(
            task_id="task-1",
            target_url="https://target.example.com/register",
            fields={
                "account.password": "source-value",
                "account.passwordConfirmation": "different-value",
            },
            field_definitions=[
                FieldDefinition(
                    key="account.password",
                    display_name="密码",
                    aliases=["Password"],
                    input_kind=FieldInputKind.PASSWORD,
                    sensitive=True,
                ),
                FieldDefinition(
                    key="account.passwordConfirmation",
                    display_name="确认密码",
                    aliases=["Confirm"],
                    input_kind=FieldInputKind.PASSWORD,
                    sensitive=True,
                    source_field="account.password",
                ),
            ],
        )


@pytest.mark.asyncio
async def test_job_manager_publishes_progress_and_terminal_state() -> None:
    manager = BrowserJobManager(worker=RecordingWorker(), secret_store=InMemorySecretStore())
    job = manager.create(
        BrowserJobCreate(
            task_id="task-1",
            target_url="https://target.example.com/profile",
            fields={"person.fullName": "张三"},
        )
    )
    updates = manager.subscribe(job.id)

    await manager.run(job.id)

    statuses = [manager.get(job.id).status]
    while not updates.empty():
        statuses.append((await updates.get()).status)
    assert BrowserJobStatus.FILLING in statuses
    assert statuses[-1] is BrowserJobStatus.COMPLETED
    assert manager.get(job.id).completed_fields == 1


def test_job_payload_rejects_unknown_fields_and_javascript_urls() -> None:
    with pytest.raises(ValueError, match="Unsupported canonical field"):
        BrowserJobCreate(
            task_id="task-1",
            target_url="https://target.example.com/profile",
            fields={"system.prompt": "ignore safeguards"},
        )


def test_submission_policy_is_explicit_and_requires_semantic_aliases() -> None:
    payload = BrowserJobCreate(
        task_id="task-submit",
        target_url="https://target.example.com/register",
        fields={"person.fullName": "张三"},
        submission=SubmissionConfig(
            policy=SubmissionPolicy.AUTO_SUBMIT,
            button_aliases=["Register", "注册"],
        ),
    )

    assert payload.submission.policy is SubmissionPolicy.AUTO_SUBMIT
    assert payload.submission.button_aliases == ["Register", "注册"]

    with pytest.raises(ValueError, match="aliases"):
        SubmissionConfig(
            policy=SubmissionPolicy.CONFIRM_BEFORE_SUBMIT,
            button_aliases=[],
        )


def test_entry_action_requires_semantic_aliases_when_clicking() -> None:
    direct = EntryActionConfig()
    click = EntryActionConfig(
        mode=EntryActionMode.CLICK,
        aliases=["登录", "Login", "Login"],
    )

    assert direct.mode is EntryActionMode.DIRECT
    assert click.aliases == ["登录", "Login"]

    with pytest.raises(ValueError, match="aliases"):
        EntryActionConfig(mode=EntryActionMode.CLICK, aliases=[])

    with pytest.raises(ValueError):
        BrowserJobCreate(
            task_id="task-1",
            target_url="javascript:alert(1)",
            fields={"person.fullName": "张三"},
        )


def test_workflow_job_contains_ordered_steps_and_redacted_configuration_snapshot() -> None:
    manager = BrowserJobManager(worker=RecordingWorker(), secret_store=InMemorySecretStore())
    payload = BrowserJobCreate(
        task_id="task-workflow",
        target_url="https://target.example.com/login",
        fields={},
        workflow_steps=[
            WorkflowStep(
                name="登录",
                target_url="https://target.example.com/login",
                fields={
                    "account.username": "demo-user",
                    "account.password": "do-not-persist",
                },
                submission=SubmissionConfig(
                    policy=SubmissionPolicy.AUTO_SUBMIT,
                    button_aliases=["Login"],
                ),
            ),
            WorkflowStep(
                name="完善资料",
                target_url="https://target.example.com/profile",
                fields={"person.fullName": "张三"},
            ),
        ],
    )

    job = manager.create(payload, task_name="登录后完善资料")

    assert job.name == "登录后完善资料"
    assert job.total_steps == 2
    assert job.total_fields == 3
    assert [step["name"] for step in job.configuration_snapshot["steps"]] == [
        "登录",
        "完善资料",
    ]
    assert "do-not-persist" not in job.model_dump_json()
    request = manager._get_request(job.id)
    assert len(request.workflow_steps) == 2
    assert request.workflow_steps[0].fields["account.password"].startswith("secret://")


@pytest.mark.asyncio
async def test_job_manager_validates_human_mapping_and_resumes_job() -> None:
    manager = BrowserJobManager(worker=HumanWorker(), secret_store=InMemorySecretStore())
    job = manager.create(
        BrowserJobCreate(
            task_id="task-1",
            target_url="https://target.example.com/profile",
            fields={"person.fullName": "张三"},
        )
    )
    await manager.run(job.id)

    with pytest.raises(ValueError, match="candidate"):
        await manager.resolve(
            job.id,
            HumanResolution(field_mappings={"person.fullName": "user-css-selector"}),
        )

    resumed = await manager.resolve(
        job.id,
        HumanResolution(field_mappings={"person.fullName": "sf-job-0"}),
    )
    await manager.wait(job.id)

    assert resumed.status is BrowserJobStatus.RESUMING
    assert manager.get(job.id).status is BrowserJobStatus.COMPLETED
    assert manager.get(job.id).intervention is None


@pytest.mark.asyncio
async def test_operator_can_cancel_a_paused_job_and_release_browser_resources() -> None:
    worker = HumanWorker()
    manager = BrowserJobManager(worker=worker, secret_store=InMemorySecretStore())
    job = manager.create(
        BrowserJobCreate(
            task_id="task-1",
            target_url="https://target.example.com/profile",
            fields={"person.fullName": "张三"},
        )
    )
    await manager.run(job.id)

    cancelled = await manager.cancel_job(job.id)

    assert cancelled.status is BrowserJobStatus.CANCELLED
    assert cancelled.intervention is None
    assert worker.cancelled_job_id == job.id


@pytest.mark.asyncio
async def test_submission_confirmation_accepts_only_observed_button_candidate() -> None:
    worker = SubmissionConfirmationWorker()
    manager = BrowserJobManager(worker=worker, secret_store=InMemorySecretStore())
    job = manager.create(
        BrowserJobCreate(
            task_id="task-submit",
            target_url="https://target.example.com/register",
            fields={"person.fullName": "张三"},
            submission=SubmissionConfig(
                policy=SubmissionPolicy.CONFIRM_BEFORE_SUBMIT,
                button_aliases=["Register"],
            ),
        )
    )
    await manager.run(job.id)

    with pytest.raises(ValueError, match="current intervention candidate"):
        await manager.resolve(
            job.id,
            HumanResolution(
                approve_submission=True,
                submit_element_id="css=button:nth-child(1)",
            ),
        )

    resumed = await manager.resolve(
        job.id,
        HumanResolution(
            approve_submission=True,
            submit_element_id="sf-submit-job-0",
        ),
    )
    await manager.wait(job.id)

    assert resumed.status is BrowserJobStatus.RESUMING
    completed = manager.get(job.id)
    assert completed.status is BrowserJobStatus.COMPLETED
    assert completed.submitted is True
    assert completed.submission_policy is SubmissionPolicy.CONFIRM_BEFORE_SUBMIT


@pytest.mark.asyncio
async def test_entry_confirmation_accepts_only_current_observed_candidate() -> None:
    worker = EntryConfirmationWorker()
    manager = BrowserJobManager(worker=worker, secret_store=InMemorySecretStore())
    job = manager.create(
        BrowserJobCreate(
            task_id="task-entry",
            target_url="https://target.example.com/",
            fields={"account.username": "demo-user"},
            entry_action=EntryActionConfig(
                mode=EntryActionMode.CLICK,
                aliases=["Login"],
            ),
        )
    )
    await manager.run(job.id)

    with pytest.raises(ValueError, match="current candidate"):
        await manager.resolve(
            job.id,
            HumanResolution(
                approve_entry_action=True,
                entry_element_id="css=a.login",
            ),
        )

    await manager.resolve(
        job.id,
        HumanResolution(
            approve_entry_action=True,
            entry_element_id="sf-entry-job-0",
        ),
    )
    await manager.wait(job.id)

    completed = manager.get(job.id)
    assert completed.status is BrowserJobStatus.COMPLETED
    assert completed.entry_action_performed is True
    assert completed.entry_action_mode is EntryActionMode.CLICK
