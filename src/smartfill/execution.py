"""Policy-gated action types shared by vision and browser adapters."""

from __future__ import annotations

from enum import StrEnum
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from smartfill.config import normalize_origin


class ActionType(StrEnum):
    NAVIGATE = "navigate"
    CLICK = "click"
    FILL = "fill"
    SELECT = "select"
    CHECK = "check"
    SCROLL = "scroll"
    WAIT = "wait"
    SUBMIT = "submit"
    REQUEST_HUMAN = "request_human"
    FINISH = "finish"


class ActionProposal(BaseModel):
    """A model-produced plan. It must never contain a plaintext secret."""

    model_config = ConfigDict(extra="forbid")

    action: ActionType
    element_id: str | None = None
    value_ref: str | None = None
    literal_value: str | None = None
    target_url: str | None = None
    option: str | None = None
    checked: bool | None = None
    confidence: float = Field(ge=0, le=1)
    evidence: str | None = Field(default=None, max_length=500)
    accessible_name: str | None = Field(default=None, max_length=300)


class PolicyDecision(BaseModel):
    allowed: bool
    reason: str
    requires_human: bool = False


class ActionPolicy:
    """Deterministic guardrail applied after every model decision."""

    def __init__(
        self,
        allowed_origins: set[str],
        *,
        minimum_confidence: float = 0.85,
        auto_submit: bool = False,
    ) -> None:
        self._allowed_origins = {normalize_origin(origin) for origin in allowed_origins}
        self._minimum_confidence = minimum_confidence
        self._auto_submit = auto_submit

    def evaluate(self, proposal: ActionProposal, *, current_url: str) -> PolicyDecision:
        try:
            current_origin = normalize_origin(current_url)
        except ValueError:
            return PolicyDecision(allowed=False, reason="Current page has an invalid origin")
        if current_origin not in self._allowed_origins:
            return PolicyDecision(allowed=False, reason="Current page origin is not approved")

        if proposal.confidence < self._minimum_confidence:
            return PolicyDecision(
                allowed=False,
                reason="Action confidence is below the configured threshold",
                requires_human=True,
            )

        if proposal.action is ActionType.NAVIGATE:
            if not proposal.target_url:
                return PolicyDecision(allowed=False, reason="Navigation requires target_url")
            try:
                target_origin = normalize_origin(proposal.target_url)
            except ValueError:
                return PolicyDecision(
                    allowed=False,
                    reason="Navigation target has an invalid origin",
                )
            if target_origin not in self._allowed_origins:
                return PolicyDecision(
                    allowed=False,
                    reason="Navigation target origin is not approved",
                )

        if proposal.action is ActionType.FILL:
            if proposal.literal_value is not None or not proposal.value_ref:
                return PolicyDecision(allowed=False, reason="Fill action requires value_ref only")
            if not proposal.value_ref.startswith(("record://", "secret://")):
                return PolicyDecision(allowed=False, reason="Unsupported value_ref scheme")
            if not proposal.element_id:
                return PolicyDecision(allowed=False, reason="Fill action requires element_id")

        if (
            proposal.action
            in {
                ActionType.CLICK,
                ActionType.SELECT,
                ActionType.CHECK,
                ActionType.SUBMIT,
            }
            and not proposal.element_id
        ):
            return PolicyDecision(allowed=False, reason="Action requires element_id")

        if proposal.action is ActionType.SUBMIT and not self._auto_submit:
            return PolicyDecision(
                allowed=True,
                reason="Submit action requires operator approval",
                requires_human=True,
            )
        return PolicyDecision(allowed=True, reason="Action passed policy checks")


def is_https_origin(value: str) -> bool:
    """Return whether a URL uses HTTPS."""

    return urlsplit(value).scheme.lower() == "https"
