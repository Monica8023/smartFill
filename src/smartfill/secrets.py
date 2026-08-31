"""Secret reference interfaces used to keep plaintext out of plans and logs."""

from __future__ import annotations

from threading import RLock
from typing import Protocol
from uuid import uuid4


class SecretStore(Protocol):
    """Minimal interface implemented by Vault/KMS adapters."""

    def put(self, name: str, value: str) -> str:
        """Store a plaintext value and return an opaque reference."""

    def resolve(self, reference: str) -> str:
        """Resolve an opaque reference immediately before browser execution."""


class InMemorySecretStore:
    """Development-only secret store. Production must use an external vault."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}
        self._lock = RLock()

    def put(self, name: str, value: str) -> str:
        if not name.strip() or not value:
            raise ValueError("Secret name and value are required")
        reference = f"secret://{uuid4()}"
        with self._lock:
            self._values[reference] = value
        return reference

    def resolve(self, reference: str) -> str:
        if not reference.startswith("secret://"):
            raise ValueError("Only secret references can be resolved")
        with self._lock:
            return self._values[reference]


def redact_value(value: str) -> str:
    """Mask a sensitive value while retaining a small correlation hint."""

    if len(value) <= 6:
        return "*" * len(value)
    return f"{value[:3]}{'*' * (len(value) - 6)}{value[-3:]}"
