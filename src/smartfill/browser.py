"""Browser driver plugin boundary and policy-gated executor."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel

from smartfill.execution import ActionPolicy, ActionProposal, ActionType, PolicyDecision
from smartfill.secrets import SecretStore
from smartfill.vision import BrowserObservation


class BrowserCommand(BaseModel):
    """Concrete command passed to a browser adapter after policy checks."""

    action: ActionType
    element_id: str | None = None
    value: str | None = None
    target_url: str | None = None
    option: str | None = None
    checked: bool | None = None


class BrowserDriver(Protocol):
    async def observe(self) -> BrowserObservation:
        """Capture DOM/a11y semantics and a screenshot."""

    async def execute(self, command: BrowserCommand) -> None:
        """Execute one concrete browser command."""


class HumanApprovalRequiredError(RuntimeError):
    """Raised when the current action must be confirmed by an operator."""


class PolicyViolationError(RuntimeError):
    """Raised when a proposed browser action violates deterministic policy."""


class SafeBrowserExecutor:
    """Resolve values only after policy approval and immediately before execution."""

    def __init__(
        self,
        driver: BrowserDriver,
        policy: ActionPolicy,
        secret_store: SecretStore,
    ) -> None:
        self._driver = driver
        self._policy = policy
        self._secret_store = secret_store

    async def execute(
        self,
        proposal: ActionProposal,
        *,
        current_url: str,
        record_values: dict[str, str],
        human_approved: bool = False,
    ) -> PolicyDecision:
        decision = self._policy.evaluate(proposal, current_url=current_url)
        if not decision.allowed:
            raise PolicyViolationError(decision.reason)
        if decision.requires_human and not human_approved:
            raise HumanApprovalRequiredError(decision.reason)

        value: str | None = None
        if proposal.value_ref:
            value = self._resolve_value(proposal.value_ref, record_values)
        await self._driver.execute(
            BrowserCommand(
                action=proposal.action,
                element_id=proposal.element_id,
                value=value,
                target_url=proposal.target_url,
                option=proposal.option,
                checked=proposal.checked,
            )
        )
        return decision

    def _resolve_value(self, reference: str, record_values: dict[str, str]) -> str:
        if reference.startswith("secret://"):
            return self._secret_store.resolve(reference)
        if reference.startswith("record://"):
            key = reference.removeprefix("record://")
            try:
                value = record_values[key]
            except KeyError as error:
                raise PolicyViolationError("Referenced record field is unavailable") from error
            if value.startswith("secret://"):
                return self._secret_store.resolve(value)
            return value
        raise PolicyViolationError("Unsupported value reference")
