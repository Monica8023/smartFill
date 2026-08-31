import pytest
from pydantic import ValidationError

from smartfill.config import Settings, normalize_origin


def test_origin_is_normalized_without_path_or_case() -> None:
    assert normalize_origin("HTTPS://Example.COM:8443/path?q=1") == "https://example.com:8443"


def test_production_requires_authentication_and_an_allowlist() -> None:
    with pytest.raises(ValidationError, match="API_TOKEN"):
        Settings(environment="production", allowed_target_origins=["https://example.com"])

    with pytest.raises(ValidationError, match="target origin"):
        Settings(environment="production", api_token="token", allowed_target_origins=[])


def test_production_rejects_insecure_target_origin() -> None:
    with pytest.raises(ValidationError, match="HTTPS"):
        Settings(
            environment="production",
            api_token="token",
            allowed_target_origins=["http://example.com"],
        )


def test_dashscope_endpoint_must_use_https() -> None:
    with pytest.raises(ValidationError, match="DashScope base URL"):
        Settings(dashscope_base_url="http://dashscope.example.com/v1")
