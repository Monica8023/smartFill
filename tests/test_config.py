import pytest
from pydantic import ValidationError

from smartfill.config import Settings, normalize_origin


def test_origin_is_normalized_without_path_or_case() -> None:
    assert normalize_origin("HTTPS://Example.COM:8443/path?q=1") == "https://example.com:8443"
    assert normalize_origin("https://Example.COM:443/path") == "https://example.com"
    assert normalize_origin("http://Example.COM:80/path") == "http://example.com"


def test_production_requires_authentication_and_an_allowlist() -> None:
    with pytest.raises(ValidationError, match="API_TOKEN"):
        Settings(
            _env_file=None,
            environment="production",
            allowed_target_origins=["https://example.com"],
        )

    with pytest.raises(ValidationError, match="target origin"):
        Settings(
            _env_file=None,
            environment="production",
            api_token="token",
            allowed_target_origins=[],
        )


def test_production_rejects_insecure_target_origin() -> None:
    with pytest.raises(ValidationError, match="HTTPS"):
        Settings(
            _env_file=None,
            environment="production",
            api_token="token",
            allowed_target_origins=["http://example.com"],
        )


def test_dashscope_endpoint_must_use_https() -> None:
    with pytest.raises(ValidationError, match="DashScope base URL"):
        Settings(_env_file=None, dashscope_base_url="http://dashscope.example.com/v1")


def test_cdp_endpoint_requires_a_supported_url() -> None:
    with pytest.raises(ValidationError, match="CDP"):
        Settings(_env_file=None, browser_cdp_url="file:///tmp/browser.sock")


def test_browser_screenshot_timeout_loads_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SMARTFILL_BROWSER_SCREENSHOT_TIMEOUT_MS", raising=False)
    assert Settings(_env_file=None).browser_screenshot_timeout_ms == 10_000

    monkeypatch.setenv("SMARTFILL_BROWSER_SCREENSHOT_TIMEOUT_MS", "25000")
    assert Settings(_env_file=None).browser_screenshot_timeout_ms == 25_000


def test_manual_login_uses_session_scoped_relaxed_https_navigation_by_default() -> None:
    settings = Settings(_env_file=None, environment="development")

    assert settings.browser_relaxed_manual_navigation is True


def test_batch_record_limit_is_bounded() -> None:
    with pytest.raises(ValidationError, match="less than or equal to 10000"):
        Settings(_env_file=None, batch_max_records=10_001)


def test_production_rejects_insecure_remote_cdp_endpoint() -> None:
    with pytest.raises(ValidationError, match="CDP"):
        Settings(
            _env_file=None,
            environment="production",
            api_token="token",
            allowed_target_origins=["https://example.com"],
            browser_cdp_url="http://browser.internal:9222",
        )

    settings = Settings(
        _env_file=None,
        environment="production",
        api_token="token",
        allowed_target_origins=["https://example.com"],
        browser_cdp_url="http://127.0.0.1:9222",
    )
    assert settings.browser_cdp_url == "http://127.0.0.1:9222"
