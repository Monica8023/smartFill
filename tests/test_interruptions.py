import pytest

from smartfill.interruptions import (
    Interruption,
    InterruptionPolicy,
    InterruptionType,
    Resolution,
)


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (InterruptionType.CAPTCHA, Resolution.NEED_HUMAN),
        (InterruptionType.MFA, Resolution.NEED_HUMAN),
        (InterruptionType.PASSWORD_ERROR, Resolution.SKIP_RECORD),
        (InterruptionType.CROSS_ORIGIN, Resolution.FATAL),
    ],
)
def test_high_risk_interruptions_are_never_auto_dismissed(
    kind: InterruptionType,
    expected: Resolution,
) -> None:
    decision = InterruptionPolicy().decide(Interruption(kind=kind))

    assert decision.resolution is expected
    assert decision.action is None


def test_ad_popup_can_be_closed_only_through_a_referenced_element() -> None:
    decision = InterruptionPolicy().decide(
        Interruption(
            kind=InterruptionType.AD_POPUP,
            dismiss_element_id="popup-close",
            confidence=0.96,
        )
    )

    assert decision.resolution is Resolution.RETRY
    assert decision.action is not None
    assert decision.action.element_id == "popup-close"


def test_ambiguous_popup_is_sent_to_a_human() -> None:
    decision = InterruptionPolicy().decide(
        Interruption(kind=InterruptionType.AD_POPUP, confidence=0.4)
    )

    assert decision.resolution is Resolution.NEED_HUMAN


def test_login_expiry_and_rate_limit_have_bounded_retries() -> None:
    policy = InterruptionPolicy(max_login_retries=1, max_rate_limit_retries=2)

    assert (
        policy.decide(Interruption(kind=InterruptionType.LOGIN_EXPIRED, attempt=0)).resolution
        is Resolution.RETRY
    )
    assert (
        policy.decide(Interruption(kind=InterruptionType.LOGIN_EXPIRED, attempt=1)).resolution
        is Resolution.NEED_HUMAN
    )
    assert (
        policy.decide(Interruption(kind=InterruptionType.RATE_LIMIT, attempt=1)).resolution
        is Resolution.RETRY
    )
    assert (
        policy.decide(Interruption(kind=InterruptionType.RATE_LIMIT, attempt=2)).resolution
        is Resolution.NEED_HUMAN
    )
