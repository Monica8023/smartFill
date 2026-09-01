"""Application configuration loaded from environment variables."""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def normalize_origin(value: str) -> str:
    """Return a canonical HTTP origin without path, query, or trailing slash."""

    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("A valid HTTP(S) origin is required")
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme.lower()}://{parsed.hostname.lower()}{port}"


class Settings(BaseSettings):
    """SmartFill runtime settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="SMARTFILL_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    environment: Literal["development", "test", "production"] = "development"
    api_token: SecretStr | None = None
    database_url: SecretStr | None = None
    allowed_target_origins: list[str] = Field(default_factory=list)
    cors_origins: list[str] = Field(default_factory=list)
    request_limit_per_minute: int = Field(default=120, ge=1, le=10_000)
    upload_max_bytes: int = Field(default=5 * 1024 * 1024, ge=1024)
    batch_max_records: int = Field(default=1_000, ge=1, le=10_000)
    dashscope_api_key: SecretStr | None = None
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    dashscope_fast_model: str = "qwen3-vl-flash"
    dashscope_strong_model: str = "qwen3-vl-plus"
    browser_headless: bool = True
    browser_cdp_url: str | None = None
    browser_artifacts_root: Path = Path("artifacts")
    browser_navigation_timeout_ms: int = Field(default=30_000, ge=1_000, le=180_000)
    browser_action_timeout_ms: int = Field(default=10_000, ge=500, le=60_000)

    @field_validator("allowed_target_origins", "cors_origins")
    @classmethod
    def normalize_origins(cls, values: list[str]) -> list[str]:
        return [normalize_origin(value) for value in values]

    @field_validator("dashscope_base_url")
    @classmethod
    def require_secure_dashscope_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("DashScope base URL must use HTTPS")
        return value.rstrip("/")

    @field_validator("browser_cdp_url")
    @classmethod
    def validate_browser_cdp_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https", "ws", "wss"} or not parsed.hostname:
            raise ValueError("Browser CDP URL must use HTTP(S) or WebSocket")
        if parsed.username or parsed.password:
            raise ValueError("Browser CDP URL cannot contain credentials")
        return value.rstrip("/")

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        url = value.get_secret_value()
        if not url.startswith(("mysql+pymysql://", "sqlite+pysqlite://")):
            raise ValueError("Database URL must use mysql+pymysql or sqlite+pysqlite")
        return value

    @model_validator(mode="after")
    def require_production_security(self) -> Settings:
        if self.environment == "production":
            if self.api_token is None:
                raise ValueError("SMARTFILL_API_TOKEN is required in production")
            if not self.allowed_target_origins:
                raise ValueError("At least one target origin is required in production")
            if any(not origin.startswith("https://") for origin in self.allowed_target_origins):
                raise ValueError("Production target origins must use HTTPS")
            if self.browser_cdp_url:
                parsed_cdp = urlsplit(self.browser_cdp_url)
                is_loopback = parsed_cdp.hostname in {"127.0.0.1", "localhost", "::1"}
                if parsed_cdp.scheme not in {"https", "wss"} and not is_loopback:
                    raise ValueError("Production Browser CDP URL must be secure or loopback-only")
        return self
