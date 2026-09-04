import time
from collections.abc import Awaitable, Callable

from fastapi.testclient import TestClient

from smartfill.api import create_app
from smartfill.browser_jobs import (
    BrowserAutomationWorker,
    BrowserJobStatus,
    BrowserRunRequest,
    BrowserRunResult,
    EntryActionMode,
    FieldCandidateSet,
    HumanIntervention,
    HumanResolution,
    InterventionCandidate,
    InterventionKind,
    JobProgress,
    PageScanRequest,
    PageScanResult,
)
from smartfill.config import Settings
from smartfill.field_schema import FieldDefinition, FieldInputKind


class ApiFakeWorker:
    async def scan_page(self, request: PageScanRequest) -> PageScanResult:
        return PageScanResult(
            initial_url=request.target_url,
            final_url="https://target.example.com/login",
            entry_action_performed=request.entry_action.mode is EntryActionMode.CLICK,
            fields=[
                FieldDefinition(
                    key="account.username",
                    display_name="用户名",
                    aliases=["账号", "username"],
                    autocomplete_hints=["username"],
                ),
                FieldDefinition(
                    key="account.password",
                    display_name="密码",
                    aliases=["密码", "password"],
                    input_kind=FieldInputKind.PASSWORD,
                    sensitive=True,
                    autocomplete_hints=["current-password"],
                ),
            ],
        )

    async def run(
        self,
        request: BrowserRunRequest,
        report: Callable[[JobProgress], Awaitable[None]],
    ) -> BrowserRunResult:
        await report(
            JobProgress(
                status=BrowserJobStatus.FILLING,
                message="正在填写",
                current_url=request.target_url,
            )
        )
        return BrowserRunResult(
            status=BrowserJobStatus.COMPLETED,
            message="完成",
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
        return None


class HumanApiWorker(ApiFakeWorker):
    async def run(
        self,
        request: BrowserRunRequest,
        report: Callable[[JobProgress], Awaitable[None]],
    ) -> BrowserRunResult:
        return BrowserRunResult(
            status=BrowserJobStatus.NEED_HUMAN,
            message="请选择姓名控件",
            current_url=request.target_url,
            intervention=HumanIntervention(
                kind=InterventionKind.FIELD_MAPPING,
                instruction="选择姓名控件",
                field_candidates=[
                    FieldCandidateSet(
                        canonical_field="person.fullName",
                        candidates=[
                            InterventionCandidate(
                                element_id="sf-api-0",
                                accessible_name="姓名",
                                role="textbox",
                                tag="input",
                                frame_path="main/profile-frame",
                                confidence=0.92,
                            )
                        ],
                    )
                ],
            ),
        )


class FailingApiWorker(ApiFakeWorker):
    async def run(
        self,
        request: BrowserRunRequest,
        report: Callable[[JobProgress], Awaitable[None]],
    ) -> BrowserRunResult:
        raise RuntimeError("synthetic API worker failure")


def make_client(
    *,
    api_token: str | None = None,
    browser_worker: BrowserAutomationWorker | None = None,
) -> TestClient:
    settings = Settings(
        _env_file=None,
        environment="development",
        api_token=api_token,
        allowed_target_origins=["https://target.example.com"],
    )
    return TestClient(create_app(settings, browser_worker=browser_worker or ApiFakeWorker()))


def test_health_and_security_headers() -> None:
    with make_client() as client:
        response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "smartfill-api"}
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"


def test_batch_import_reuses_a_workflow_and_tracks_each_record() -> None:
    with make_client() as client:
        task = client.post(
            "/api/v1/tasks",
            json={
                "name": "模板任务",
                "target_origin": "https://target.example.com",
                "record_count": 1,
            },
        ).json()
        client.post(f"/api/v1/tasks/{task['id']}/validate")
        client.post(f"/api/v1/tasks/{task['id']}/start")
        template = client.post(
            "/api/v1/browser/jobs",
            json={
                "task_id": task["id"],
                "target_url": "https://target.example.com/profile",
                "fields": {
                    "account.username": "template-user",
                    "account.password": "template-password",
                    "person.fullName": "Template User",
                },
            },
        ).json()

        response = client.post(
            "/api/v1/batches",
            data={
                "name": "九月用户导入",
                "workflow_job_id": template["id"],
                "mapping_json": (
                    '{"username":"account.username",'
                    '"password":"account.password",'
                    '"full_name":"person.fullName"}'
                ),
            },
            files={
                "file": (
                    "people.csv",
                    b"username,password,full_name\nalice,p1,Alice\nbob,p2,Bob\n",
                    "text/csv",
                )
            },
        )
        assert response.status_code == 202
        batch_id = response.json()["id"]
        current = response.json()
        for _ in range(100):
            current = client.get(f"/api/v1/batches/{batch_id}").json()
            if current["status"] in {"completed", "completed_with_errors", "failed"}:
                break
            time.sleep(0.01)

        assert current["status"] == "completed"
        assert current["completed_records"] == 2
        assert len(current["items"]) == 2
        assert all(item["browser_job_id"] for item in current["items"])
        assert client.get("/api/v1/batches").json()[0]["id"] == batch_id


def test_batch_import_rejects_incomplete_workflow_mapping() -> None:
    with make_client() as client:
        task = client.post(
            "/api/v1/tasks",
            json={
                "name": "模板任务",
                "target_origin": "https://target.example.com",
                "record_count": 1,
            },
        ).json()
        client.post(f"/api/v1/tasks/{task['id']}/validate")
        client.post(f"/api/v1/tasks/{task['id']}/start")
        template = client.post(
            "/api/v1/browser/jobs",
            json={
                "task_id": task["id"],
                "target_url": "https://target.example.com/profile",
                "fields": {
                    "account.username": "template-user",
                    "account.password": "template-password",
                },
            },
        ).json()

        response = client.post(
            "/api/v1/batches",
            data={
                "name": "错误导入",
                "workflow_job_id": template["id"],
                "mapping_json": '{"username":"account.username"}',
            },
            files={"file": ("people.csv", b"username\nalice\n", "text/csv")},
        )

    assert response.status_code == 422
    assert "Missing field mappings" in response.json()["detail"]


def test_openapi_docs_allow_only_the_required_swagger_assets() -> None:
    with make_client() as client:
        response = client.get("/api/docs")

    assert response.status_code == 200
    assert "SwaggerUIBundle" in response.text
    policy = response.headers["content-security-policy"]
    assert "script-src https://cdn.jsdelivr.net" in policy
    assert "connect-src 'self'" in policy


def test_web_console_and_semantic_demo_target_are_served() -> None:
    with make_client() as client:
        console = client.get("/")
        target = client.get("/demo/target")
        home = client.get("/demo/home")
        login = client.get("/demo/login")
        login_submit = client.post("/demo/login-success")
        frame = client.get("/demo/frame")
        shadow_script = client.get("/demo/shadow.js")
        ambiguous = client.get("/demo/ambiguous")

    assert console.status_code == 200
    assert '<div id="root"></div>' in console.text
    assert "default-src 'self'" in console.headers["content-security-policy"]
    assert target.status_code == 200
    assert home.status_code == 200
    assert 'href="/demo/login"' in home.text
    assert login.status_code == 200
    assert 'autocomplete="username"' in login.text
    assert 'method="post"' in login.text
    assert login_submit.status_code == 200
    assert 'name="username"' in target.text
    assert 'src="/demo/frame"' in target.text
    assert 'src="/demo/shadow.js"' in target.text
    assert "活动广告" in target.text
    assert frame.status_code == 200
    assert "身份证号码" in frame.text
    assert frame.headers["x-frame-options"] == "SAMEORIGIN"
    assert "frame-ancestors 'self'" in frame.headers["content-security-policy"]
    assert shadow_script.status_code == 200
    assert "attachShadow" in shadow_script.text
    assert ambiguous.status_code == 200
    assert ambiguous.text.count("姓名") == 2


def test_task_api_happy_path() -> None:
    with make_client() as client:
        created = client.post(
            "/api/v1/tasks",
            json={
                "name": "批量完善用户资料",
                "target_origin": "https://target.example.com",
                "record_count": 10,
            },
        )
        assert created.status_code == 201
        task_id = created.json()["id"]
        assert created.json()["status"] == "draft"

        assert client.post(f"/api/v1/tasks/{task_id}/validate").json()["status"] == "ready"
        assert client.post(f"/api/v1/tasks/{task_id}/start").json()["status"] == "running"
        assert client.post(f"/api/v1/tasks/{task_id}/pause").json()["status"] == "paused"
        assert client.post(f"/api/v1/tasks/{task_id}/resume").json()["status"] == "running"


def test_task_api_rejects_unapproved_and_insecure_origins() -> None:
    with make_client() as client:
        insecure = client.post(
            "/api/v1/tasks",
            json={"name": "bad", "target_origin": "http://target.example.com", "record_count": 1},
        )
        unapproved = client.post(
            "/api/v1/tasks",
            json={"name": "bad", "target_origin": "https://other.example.com", "record_count": 1},
        )

    assert insecure.status_code == 422
    assert unapproved.status_code == 403


def test_api_token_is_required_when_configured() -> None:
    with make_client(api_token="local-test-token") as client:
        denied = client.get("/api/v1/tasks")
        allowed = client.get("/api/v1/tasks", headers={"Authorization": "Bearer local-test-token"})

    assert denied.status_code == 401
    assert allowed.status_code == 200


def test_invalid_transition_returns_conflict_without_internal_details() -> None:
    with make_client() as client:
        created = client.post(
            "/api/v1/tasks",
            json={
                "name": "test",
                "target_origin": "https://target.example.com",
                "record_count": 1,
            },
        ).json()
        response = client.post(f"/api/v1/tasks/{created['id']}/start")

    assert response.status_code == 409
    assert response.json() == {"detail": "Task state does not allow this operation"}


def test_import_preview_endpoint_never_returns_plaintext_secrets() -> None:
    content = (
        "username,password,full_name,id_number\nzhangsan,P@ssw0rd!,张三,110101199001011234\n"
    ).encode()
    mapping = {
        "username": "account.username",
        "password": "account.password",
        "full_name": "person.fullName",
        "id_number": "person.idNumber",
    }

    with make_client() as client:
        response = client.post(
            "/api/v1/imports/preview",
            files={"file": ("people.csv", content, "text/csv")},
            data={"mapping_json": __import__("json").dumps(mapping)},
        )

    assert response.status_code == 200
    serialized = response.text
    assert "P@ssw0rd!" not in serialized
    assert "110101199001011234" not in serialized
    assert "secret://" in serialized


def test_unknown_task_returns_a_generic_not_found_response() -> None:
    with make_client() as client:
        response = client.get("/api/v1/tasks/missing")

    assert response.status_code == 404
    assert response.json() == {"detail": "Task not found"}


def test_browser_job_api_runs_and_never_returns_plaintext_sensitive_values() -> None:
    with make_client() as client:
        task = client.post(
            "/api/v1/tasks",
            json={
                "name": "浏览器联调",
                "target_origin": "https://target.example.com",
                "record_count": 1,
            },
        ).json()
        client.post(f"/api/v1/tasks/{task['id']}/validate")
        client.post(f"/api/v1/tasks/{task['id']}/start")

        response = client.post(
            "/api/v1/browser/jobs",
            json={
                "task_id": task["id"],
                "target_url": "https://target.example.com/profile",
                "fields": {
                    "person.fullName": "张三",
                    "account.password": "P@ssw0rd!",
                    "person.idNumber": "110101199001011234",
                },
            },
        )
        assert response.status_code == 202
        job_id = response.json()["id"]
        job = client.get(f"/api/v1/browser/jobs/{job_id}")

    assert job.status_code == 200
    assert job.json()["status"] == "completed"
    assert job.json()["completed_fields"] == 3
    assert "P@ssw0rd!" not in job.text
    assert "110101199001011234" not in job.text


def test_browser_job_failure_updates_task_and_exposes_diagnostic_download() -> None:
    with make_client(browser_worker=FailingApiWorker()) as client:
        task = client.post(
            "/api/v1/tasks",
            json={
                "name": "失败状态同步",
                "target_origin": "https://target.example.com",
                "record_count": 1,
            },
        ).json()
        client.post(f"/api/v1/tasks/{task['id']}/validate")
        client.post(f"/api/v1/tasks/{task['id']}/start")

        response = client.post(
            "/api/v1/browser/jobs",
            json={
                "task_id": task["id"],
                "target_url": "https://target.example.com/profile",
                "fields": {"person.fullName": "张三"},
            },
        )
        job_id = response.json()["id"]
        current = response.json()
        for _ in range(100):
            current = client.get(f"/api/v1/browser/jobs/{job_id}").json()
            if current["status"] == "failed":
                break
            time.sleep(0.01)

        failed_task = client.get(f"/api/v1/tasks/{task['id']}").json()
        diagnostic = client.get(current["diagnostic_url"])

    assert current["status"] == "failed"
    assert current["diagnostic_id"] in current["message"]
    assert failed_task["status"] == "failed"
    assert diagnostic.status_code == 200
    assert "RuntimeError: synthetic API worker failure" in diagnostic.text


def test_browser_job_api_accepts_task_scoped_dynamic_field_schema() -> None:
    with make_client() as client:
        task = client.post(
            "/api/v1/tasks",
            json={
                "name": "动态字段联调",
                "target_origin": "https://target.example.com",
                "record_count": 1,
            },
        ).json()
        client.post(f"/api/v1/tasks/{task['id']}/validate")
        client.post(f"/api/v1/tasks/{task['id']}/start")

        response = client.post(
            "/api/v1/browser/jobs",
            json={
                "task_id": task["id"],
                "target_url": "https://target.example.com/register",
                "fields": {
                    "person.firstName": "San",
                    "person.ssn": "123-45-6789",
                },
                "field_definitions": [
                    {
                        "key": "person.firstName",
                        "display_name": "名",
                        "aliases": ["First Name", "Given Name"],
                    },
                    {
                        "key": "person.ssn",
                        "display_name": "SSN",
                        "aliases": ["SSN"],
                        "sensitive": True,
                    },
                ],
            },
        )

    assert response.status_code == 202
    assert response.json()["field_names"] == ["person.firstName", "person.ssn"]
    assert "123-45-6789" not in response.text


def test_page_scan_api_discovers_login_fields_after_clicking_login_entry() -> None:
    with make_client() as client:
        response = client.post(
            "/api/v1/browser/page-scan",
            json={
                "target_url": "https://target.example.com/",
                "entry_action": {
                    "mode": "click",
                    "aliases": ["登录", "Login"],
                },
            },
        )

    assert response.status_code == 200
    result = response.json()
    assert result["entry_action_performed"] is True
    assert result["final_url"] == "https://target.example.com/login"
    assert [field["key"] for field in result["fields"]] == [
        "account.username",
        "account.password",
    ]
    assert result["fields"][1]["sensitive"] is True


def test_page_scan_api_rejects_unapproved_target_origin_before_worker_call() -> None:
    with make_client() as client:
        response = client.post(
            "/api/v1/browser/page-scan",
            json={"target_url": "https://unapproved.example.com/login"},
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "Target origin is not approved"}


def test_target_origin_settings_apply_immediately_without_restart() -> None:
    with make_client() as client:
        before = client.post(
            "/api/v1/browser/page-scan",
            json={"target_url": "https://new-target.example.com/login"},
        )
        updated = client.put(
            "/api/v1/settings/target-origins",
            json={"origins": ["https://new-target.example.com"]},
        )
        after = client.post(
            "/api/v1/browser/page-scan",
            json={"target_url": "https://new-target.example.com/login"},
        )

    assert before.status_code == 403
    assert updated.status_code == 200
    assert updated.json()["origins"] == ["https://new-target.example.com"]
    assert after.status_code == 200


def test_browser_job_rejects_task_origin_mismatch() -> None:
    with make_client() as client:
        task = client.post(
            "/api/v1/tasks",
            json={
                "name": "浏览器联调",
                "target_origin": "https://target.example.com",
                "record_count": 1,
            },
        ).json()
        response = client.post(
            "/api/v1/browser/jobs",
            json={
                "task_id": task["id"],
                "target_url": "https://other.example.com/profile",
                "fields": {"person.fullName": "张三"},
            },
        )

    assert response.status_code == 403


def test_browser_job_websocket_authenticates_before_streaming_state() -> None:
    token = "local-test-token"
    with make_client(api_token=token) as client:
        task = client.post(
            "/api/v1/tasks",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "name": "实时状态",
                "target_origin": "https://target.example.com",
                "record_count": 1,
            },
        ).json()
        client.post(
            f"/api/v1/tasks/{task['id']}/validate",
            headers={"Authorization": f"Bearer {token}"},
        )
        client.post(
            f"/api/v1/tasks/{task['id']}/start",
            headers={"Authorization": f"Bearer {token}"},
        )
        created = client.post(
            "/api/v1/browser/jobs",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "task_id": task["id"],
                "target_url": "https://target.example.com/profile",
                "fields": {"person.fullName": "张三"},
            },
        ).json()

        with client.websocket_connect(
            f"/api/v1/browser/jobs/{created['id']}/stream"
        ) as websocket:
            websocket.send_json({"token": token})
            update = websocket.receive_json()

    assert update["id"] == created["id"]
    assert update["status"] == "completed"


def test_human_confirmation_api_accepts_only_current_candidates_and_resumes() -> None:
    with make_client(browser_worker=HumanApiWorker()) as client:
        task = client.post(
            "/api/v1/tasks",
            json={
                "name": "人工确认联调",
                "target_origin": "https://target.example.com",
                "record_count": 1,
            },
        ).json()
        client.post(f"/api/v1/tasks/{task['id']}/validate")
        client.post(f"/api/v1/tasks/{task['id']}/start")
        created = client.post(
            "/api/v1/browser/jobs",
            json={
                "task_id": task["id"],
                "target_url": "https://target.example.com/profile",
                "fields": {"person.fullName": "张三"},
            },
        ).json()
        waiting = client.get(f"/api/v1/browser/jobs/{created['id']}").json()

        rejected = client.post(
            f"/api/v1/browser/jobs/{created['id']}/resolve",
            json={"field_mappings": {"person.fullName": "css=input:nth-child(1)"}},
        )
        accepted = client.post(
            f"/api/v1/browser/jobs/{created['id']}/resolve",
            json={"field_mappings": {"person.fullName": "sf-api-0"}},
        )
        completed = client.get(f"/api/v1/browser/jobs/{created['id']}")

    assert waiting["status"] == "need_human"
    assert waiting["intervention"]["field_candidates"][0]["candidates"][0] == {
        "element_id": "sf-api-0",
        "accessible_name": "姓名",
        "role": "textbox",
        "tag": "input",
        "frame_path": "main/profile-frame",
        "confidence": 0.92,
    }
    assert rejected.status_code == 422
    assert accepted.status_code == 202
    assert completed.json()["status"] == "completed"
