from smartfill.execution import ActionPolicy, ActionProposal, ActionType


def test_fill_requires_a_secret_or_record_value_reference() -> None:
    policy = ActionPolicy(allowed_origins={"https://target.example.com"})

    decision = policy.evaluate(
        ActionProposal(
            action=ActionType.FILL,
            element_id="field-1",
            value_ref="record://person.fullName",
            confidence=0.97,
        ),
        current_url="https://target.example.com/profile",
    )

    assert decision.allowed is True
    assert decision.requires_human is False


def test_fill_rejects_literal_values_even_when_the_origin_is_allowed() -> None:
    policy = ActionPolicy(allowed_origins={"https://target.example.com"})

    decision = policy.evaluate(
        ActionProposal(
            action=ActionType.FILL,
            element_id="field-1",
            literal_value="a secret value",
            confidence=0.99,
        ),
        current_url="https://target.example.com/profile",
    )

    assert decision.allowed is False
    assert "value_ref" in decision.reason


def test_navigation_to_an_unapproved_origin_is_blocked() -> None:
    policy = ActionPolicy(allowed_origins={"https://target.example.com"})

    decision = policy.evaluate(
        ActionProposal(
            action=ActionType.NAVIGATE,
            target_url="https://ads.example.net/landing",
            confidence=1.0,
        ),
        current_url="https://target.example.com/profile",
    )

    assert decision.allowed is False
    assert "origin" in decision.reason


def test_submit_is_routed_to_human_confirmation_by_default() -> None:
    policy = ActionPolicy(allowed_origins={"https://target.example.com"})

    decision = policy.evaluate(
        ActionProposal(
            action=ActionType.SUBMIT,
            element_id="save-profile",
            confidence=0.99,
        ),
        current_url="https://target.example.com/profile",
    )

    assert decision.allowed is True
    assert decision.requires_human is True


def test_low_confidence_action_is_not_executed() -> None:
    policy = ActionPolicy(allowed_origins={"https://target.example.com"})

    decision = policy.evaluate(
        ActionProposal(action=ActionType.CLICK, element_id="close", confidence=0.42),
        current_url="https://target.example.com/profile",
    )

    assert decision.allowed is False
    assert decision.requires_human is True
