from fastapi.testclient import TestClient

from smartfill.api import create_app
from smartfill.config import Settings


def make_client(*, api_token: str | None = None) -> TestClient:
    settings = Settings(
        environment="development",
        api_token=api_token,
        allowed_target_origins=["https://target.example.com"],
    )
    return TestClient(create_app(settings))


def test_health_and_security_headers() -> None:
    with make_client() as client:
        response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "smartfill-api"}
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"


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
