import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi.testclient import TestClient

from smartfill.api import create_app
from smartfill.browser_jobs import (
    BrowserAutomationWorker,
    BrowserJobStatus,
    BrowserRunRequest,
    BrowserRunResult,
    ExecutionStatistics,
    HumanIntervention,
    HumanResolution,
    InterventionKind,
    JobProgress,
)
from smartfill.config import Settings


class ApiFakeWorker:
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
            message="页面需要人工处理",
            current_url=request.target_url,
            intervention=HumanIntervention(
                kind=InterventionKind.VISUAL_REVIEW,
                instruction="处理页面状态后继续视觉识别",
                requires_browser_interaction=True,
            ),
        )


class StatisticsApiWorker(ApiFakeWorker):
    async def run(
        self,
        request: BrowserRunRequest,
        report: Callable[[JobProgress], Awaitable[None]],
    ) -> BrowserRunResult:
        return BrowserRunResult(
            status=BrowserJobStatus.COMPLETED,
            message="目标入口已找到",
            current_url=request.target_url,
            statistics=ExecutionStatistics(
                duration_ms=1_500,
                screenshot_count=2,
                model_call_count=2,
                model_latency_ms=900,
                browser_action_count=1,
                click_count=1,
            ),
        )


class DownloadApiWorker(ApiFakeWorker):
    def __init__(self, download_path: Path) -> None:
        self.download_path = download_path

    async def run(
        self,
        request: BrowserRunRequest,
        report: Callable[[JobProgress], Awaitable[None]],
    ) -> BrowserRunResult:
        return BrowserRunResult(
            status=BrowserJobStatus.COMPLETED,
            message="文档下载完成",
            current_url=request.target_url,
            download_paths=[str(self.download_path)],
        )


class FailingApiWorker(ApiFakeWorker):
    async def run(
        self,
        request: BrowserRunRequest,
        report: Callable[[JobProgress], Awaitable[None]],
    ) -> BrowserRunResult:
        raise RuntimeError("synthetic API worker failure")


class RetainedApiWorker(ApiFakeWorker):
    def __init__(self) -> None:
        self.closed_job_id: str | None = None

    async def run(
        self,
        request: BrowserRunRequest,
        report: Callable[[JobProgress], Awaitable[None]],
    ) -> BrowserRunResult:
        return BrowserRunResult(
            status=BrowserJobStatus.COMPLETED,
            message="完成, 浏览器保持打开",
            current_url=request.target_url,
            completed_fields=len(request.fields),
            browser_session_open=True,
        )

    async def cancel(self, job_id: str) -> None:
        self.closed_job_id = job_id


def make_client(
    *,
    api_token: str | None = None,
    browser_worker: BrowserAutomationWorker | None = None,
    artifacts_root: Path | None = None,
) -> TestClient:
    settings = Settings(
        _env_file=None,
        environment="development",
        api_token=api_token,
        allowed_target_origins=["https://target.example.com"],
        browser_artifacts_root=artifacts_root or Path("artifacts"),
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


def test_browser_job_api_returns_active_manual_login_job_for_duplicate_launch() -> None:
    with make_client(browser_worker=HumanApiWorker()) as client:
        task_ids: list[str] = []
        for suffix in ("1", "2"):
            task = client.post(
                "/api/v1/tasks",
                json={
                    "name": f"人工登录任务 {suffix}",
                    "target_origin": "https://target.example.com",
                    "record_count": 1,
                },
            ).json()
            client.post(f"/api/v1/tasks/{task['id']}/validate")
            client.post(f"/api/v1/tasks/{task['id']}/start")
            task_ids.append(task["id"])

        payload = {
            "target_url": "https://target.example.com/portal",
            "fields": {},
            "target_intent": "等待用户登录",
            "authentication_mode": "manual",
            "authentication_session_key": "property-account",
            "heartbeat_url": "https://target.example.com/session/ping",
        }
        first = client.post(
            "/api/v1/browser/jobs",
            json={**payload, "task_id": task_ids[0]},
        ).json()
        for _ in range(100):
            current = client.get(f"/api/v1/browser/jobs/{first['id']}").json()
            if current["status"] == "need_human":
                break
            time.sleep(0.01)

        duplicate_response = client.post(
            "/api/v1/browser/jobs",
            json={**payload, "task_id": task_ids[1]},
        )

    assert duplicate_response.status_code == 202
    assert duplicate_response.json()["id"] == first["id"]
    assert duplicate_response.json()["status"] == "need_human"
    assert duplicate_response.json()["diagnostic_id"] is None


def test_operator_can_close_a_browser_after_the_job_completes() -> None:
    worker = RetainedApiWorker()
    with make_client(browser_worker=worker) as client:
        task = client.post(
            "/api/v1/tasks",
            json={
                "name": "保留浏览器",
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
                "keep_browser_open": True,
            },
        ).json()
        current = created
        for _ in range(100):
            current = client.get(f"/api/v1/browser/jobs/{created['id']}").json()
            if current["status"] == "completed":
                break
            time.sleep(0.01)

        response = client.post(
            f"/api/v1/browser/jobs/{created['id']}/browser/close"
        )

    assert current["browser_session_open"] is True
    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert response.json()["browser_session_open"] is False
    assert worker.closed_job_id == created["id"]


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


def test_completed_browser_job_exposes_execution_statistics_log(tmp_path: Path) -> None:
    with make_client(
        browser_worker=StatisticsApiWorker(),
        artifacts_root=tmp_path,
    ) as client:
        task = client.post(
            "/api/v1/tasks",
            json={
                "name": "入口定位统计",
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
                "target_url": "https://target.example.com/portal",
                "fields": {},
                "target_intent": "找到社保卡应用状态查询入口并点击",
            },
        ).json()
        current = created
        for _ in range(100):
            current = client.get(f"/api/v1/browser/jobs/{created['id']}").json()
            if current["status"] == "completed":
                break
            time.sleep(0.01)

        statistics = client.get(current["statistics_url"])

    assert current["statistics"]["duration_ms"] == 1_500
    assert current["statistics"]["screenshot_count"] == 2
    assert statistics.status_code == 200
    assert "model_call_count: 2" in statistics.text
    assert "browser_action_count: 1" in statistics.text


def test_completed_browser_job_exposes_downloaded_document(tmp_path: Path) -> None:
    download = tmp_path / "jobs" / "worker" / "downloads" / "guide.txt"
    download.parent.mkdir(parents=True)
    download.write_text("guide content", encoding="utf-8")
    with make_client(
        browser_worker=DownloadApiWorker(download),
        artifacts_root=tmp_path,
    ) as client:
        task = client.post(
            "/api/v1/tasks",
            json={
                "name": "下载办事指南",
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
                "target_url": "https://target.example.com/guide",
                "fields": {},
                "target_intent": "进入办事指南并下载文档",
            },
        ).json()
        current = created
        for _ in range(100):
            current = client.get(f"/api/v1/browser/jobs/{created['id']}").json()
            if current["status"] == "completed":
                break
            time.sleep(0.01)

        downloaded = client.get(current["download_urls"][0])

    assert downloaded.status_code == 200
    assert downloaded.text == "guide content"
    assert 'filename="guide.txt"' in downloaded.headers["content-disposition"]


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


def test_legacy_page_scan_api_is_removed() -> None:
    with make_client() as client:
        response = client.post(
            "/api/v1/browser/page-scan",
            json={"target_url": "https://target.example.com/"},
        )

    assert response.status_code == 404


def test_target_origin_settings_apply_immediately_without_restart() -> None:
    with make_client() as client:
        before = client.get("/api/v1/settings/target-origins")
        updated = client.put(
            "/api/v1/settings/target-origins",
            json={"origins": ["https://new-target.example.com"]},
        )
        after = client.get("/api/v1/settings/target-origins")

    assert before.status_code == 200
    assert updated.status_code == 200
    assert updated.json()["origins"] == ["https://new-target.example.com"]
    assert after.status_code == 200
    assert after.json()["origins"] == ["https://new-target.example.com"]


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


def test_visual_review_api_resumes_without_dom_mapping_payload() -> None:
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
            json={"field_mappings": {"person.fullName": "css=input"}},
        )
        accepted = client.post(
            f"/api/v1/browser/jobs/{created['id']}/resolve",
            json={},
        )
        completed = client.get(f"/api/v1/browser/jobs/{created['id']}")

    assert waiting["status"] == "need_human"
    assert waiting["intervention"] == {
        "kind": "visual_review",
        "instruction": "处理页面状态后继续视觉识别",
        "submission_candidates": [],
        "entry_candidates": [],
        "requires_browser_interaction": True,
        "missing_fields": [],
    }
    assert rejected.status_code == 422
    assert accepted.status_code == 202
    assert completed.json()["status"] == "completed"
