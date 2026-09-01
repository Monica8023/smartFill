"""Runtime-editable settings backed by the system settings repository."""

from __future__ import annotations

from datetime import UTC, datetime
from threading import RLock
from typing import Protocol
from urllib.parse import urlsplit

from smartfill.config import normalize_origin
from smartfill.persistence import TargetOriginSettings


class TargetOriginSettingsRepository(Protocol):
    def get_target_origins(self) -> TargetOriginSettings | None: ...

    def replace_target_origins(self, origins: list[str]) -> TargetOriginSettings: ...


class InMemorySystemSettingsRepository:
    def __init__(self) -> None:
        self._settings: TargetOriginSettings | None = None
        self._lock = RLock()

    def get_target_origins(self) -> TargetOriginSettings | None:
        with self._lock:
            return self._settings

    def replace_target_origins(self, origins: list[str]) -> TargetOriginSettings:
        settings = TargetOriginSettings(origins=origins, updated_at=datetime.now(UTC))
        with self._lock:
            self._settings = settings
        return settings


class TargetOriginService:
    def __init__(
        self,
        repository: TargetOriginSettingsRepository,
        initial_origins: set[str],
    ) -> None:
        self._repository = repository
        existing = repository.get_target_origins()
        if existing is None:
            existing = repository.replace_target_origins(
                sorted(self._normalize_all(initial_origins))
            )
        self._origins = set(existing.origins)
        self._updated_at = existing.updated_at
        self._lock = RLock()

    def get(self) -> TargetOriginSettings:
        with self._lock:
            return TargetOriginSettings(
                origins=sorted(self._origins),
                updated_at=self._updated_at,
            )

    def replace(self, origins: list[str]) -> TargetOriginSettings:
        normalized = sorted(self._normalize_all(origins))
        if not normalized:
            raise ValueError("At least one target origin is required")
        saved = self._repository.replace_target_origins(normalized)
        with self._lock:
            self._origins = set(saved.origins)
            self._updated_at = saved.updated_at
        return self.get()

    def contains(self, url_or_origin: str) -> bool:
        try:
            origin = normalize_origin(url_or_origin)
        except ValueError:
            return False
        with self._lock:
            return origin in self._origins

    def origins(self) -> set[str]:
        with self._lock:
            return set(self._origins)

    @staticmethod
    def _normalize_all(origins: set[str] | list[str]) -> set[str]:
        normalized: set[str] = set()
        for value in origins:
            origin = normalize_origin(value)
            parsed = urlsplit(origin)
            if parsed.scheme != "https" and parsed.hostname not in {
                "127.0.0.1",
                "localhost",
                "::1",
            }:
                raise ValueError("Non-loopback target origins must use HTTPS")
            normalized.add(origin)
        return normalized
