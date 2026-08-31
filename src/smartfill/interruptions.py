"""Deterministic recovery policy for unexpected browser states."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from smartfill.execution import ActionProposal, ActionType


class InterruptionType(StrEnum):
    COOKIE_BANNER = "cookie_banner"
    AD_POPUP = "ad_popup"
    NOTIFICATION_PROMPT = "notification_prompt"
    LOGIN_EXPIRED = "login_expired"
    CAPTCHA = "captcha"
    MFA = "mfa"
    PASSWORD_ERROR = "password_error"
    RATE_LIMIT = "rate_limit"
    CROSS_ORIGIN = "cross_origin"
    FIELD_AMBIGUITY = "field_ambiguity"
    BUSINESS_VALIDATION = "business_validation"
    UNKNOWN = "unknown"


class Resolution(StrEnum):
    RESOLVED = "resolved"
    RETRY = "retry"
    SKIP_RECORD = "skip_record"
    NEED_HUMAN = "need_human"
    FATAL = "fatal"


class Interruption(BaseModel):
    kind: InterruptionType
    dismiss_element_id: str | None = None
    confidence: float = Field(default=1.0, ge=0, le=1)
    attempt: int = Field(default=0, ge=0)


class InterruptionDecision(BaseModel):
    resolution: Resolution
    reason: str
    action: ActionProposal | None = None


class InterruptionPolicy:
    """Map detected interruptions to bounded, auditable recovery behavior."""

    def __init__(
        self,
        *,
        minimum_dismiss_confidence: float = 0.9,
        max_login_retries: int = 1,
        max_rate_limit_retries: int = 3,
    ) -> None:
        self._minimum_dismiss_confidence = minimum_dismiss_confidence
        self._max_login_retries = max_login_retries
        self._max_rate_limit_retries = max_rate_limit_retries

    def decide(self, interruption: Interruption) -> InterruptionDecision:
        if interruption.kind is InterruptionType.CROSS_ORIGIN:
            return InterruptionDecision(
                resolution=Resolution.FATAL,
                reason="Unexpected cross-origin navigation",
            )
        if interruption.kind in {InterruptionType.CAPTCHA, InterruptionType.MFA}:
            return InterruptionDecision(
                resolution=Resolution.NEED_HUMAN,
                reason="Human verification is required",
            )
        if interruption.kind is InterruptionType.PASSWORD_ERROR:
            return InterruptionDecision(
                resolution=Resolution.SKIP_RECORD,
                reason="Password failures are not retried automatically",
            )
        if interruption.kind is InterruptionType.LOGIN_EXPIRED:
            return self._bounded_retry(
                interruption.attempt,
                self._max_login_retries,
                "Login session expired",
            )
        if interruption.kind is InterruptionType.RATE_LIMIT:
            return self._bounded_retry(
                interruption.attempt,
                self._max_rate_limit_retries,
                "Target site rate limit detected",
            )
        if interruption.kind in {
            InterruptionType.COOKIE_BANNER,
            InterruptionType.AD_POPUP,
            InterruptionType.NOTIFICATION_PROMPT,
        }:
            return self._dismiss(interruption)
        if interruption.kind is InterruptionType.BUSINESS_VALIDATION:
            return InterruptionDecision(
                resolution=Resolution.SKIP_RECORD,
                reason="Target site rejected the record data",
            )
        return InterruptionDecision(
            resolution=Resolution.NEED_HUMAN,
            reason="Interruption cannot be resolved safely",
        )

    def _dismiss(self, interruption: Interruption) -> InterruptionDecision:
        if (
            interruption.dismiss_element_id is None
            or interruption.confidence < self._minimum_dismiss_confidence
        ):
            return InterruptionDecision(
                resolution=Resolution.NEED_HUMAN,
                reason="Dismiss action is ambiguous",
            )
        return InterruptionDecision(
            resolution=Resolution.RETRY,
            reason="Dismiss the interruption and observe the page again",
            action=ActionProposal(
                action=ActionType.CLICK,
                element_id=interruption.dismiss_element_id,
                confidence=interruption.confidence,
            ),
        )

    @staticmethod
    def _bounded_retry(attempt: int, maximum: int, reason: str) -> InterruptionDecision:
        if attempt < maximum:
            return InterruptionDecision(resolution=Resolution.RETRY, reason=reason)
        return InterruptionDecision(
            resolution=Resolution.NEED_HUMAN,
            reason=f"{reason}; automatic retry limit reached",
        )
