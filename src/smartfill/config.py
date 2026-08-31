"""Application configuration loaded from environment variables."""

from __future__ import annotations

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
    allowed_target_origins: list[str] = Field(default_factory=list)
    cors_origins: list[str] = Field(default_factory=list)
    request_limit_per_minute: int = Field(default=120, ge=1, le=10_000)
    upload_max_bytes: int = Field(default=5 * 1024 * 1024, ge=1024)
    dashscope_api_key: SecretStr | None = None
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    dashscope_fast_model: str = "qwen3-vl-flash"
    dashscope_strong_model: str = "qwen3-vl-plus"

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

    @model_validator(mode="after")
    def require_production_security(self) -> Settings:
        if self.environment == "production":
            if self.api_token is None:
                raise ValueError("SMARTFILL_API_TOKEN is required in production")
            if not self.allowed_target_origins:
                raise ValueError("At least one target origin is required in production")
            if any(not origin.startswith("https://") for origin in self.allowed_target_origins):
                raise ValueError("Production target origins must use HTTPS")
        return self
