import pytest

from smartfill.browser import (
    BrowserCommand,
    HumanApprovalRequiredError,
    PolicyViolationError,
    SafeBrowserExecutor,
)
from smartfill.execution import ActionPolicy, ActionProposal, ActionType
from smartfill.secrets import InMemorySecretStore


class RecordingDriver:
    def __init__(self) -> None:
        self.commands: list[BrowserCommand] = []

    async def execute(self, command: BrowserCommand) -> None:
        self.commands.append(command)


def make_executor() -> tuple[SafeBrowserExecutor, RecordingDriver, InMemorySecretStore]:
    driver = RecordingDriver()
    store = InMemorySecretStore()
    executor = SafeBrowserExecutor(
        driver=driver,
        policy=ActionPolicy({"https://target.example.com"}),
        secret_store=store,
    )
    return executor, driver, store


@pytest.mark.asyncio
async def test_executor_resolves_secret_only_after_policy_approval() -> None:
    executor, driver, store = make_executor()
    reference = store.put("record-1/account.password", "P@ssw0rd!")

    await executor.execute(
        ActionProposal(
            action=ActionType.FILL,
            element_id="password",
            value_ref=reference,
            confidence=0.99,
        ),
        current_url="https://target.example.com/login",
        record_values={},
    )

    assert driver.commands == [
        BrowserCommand(action=ActionType.FILL, element_id="password", value="P@ssw0rd!")
    ]


@pytest.mark.asyncio
async def test_executor_resolves_non_secret_record_fields() -> None:
    executor, driver, _ = make_executor()

    await executor.execute(
        ActionProposal(
            action=ActionType.FILL,
            element_id="full-name",
            value_ref="record://person.fullName",
            confidence=0.95,
        ),
        current_url="https://target.example.com/profile",
        record_values={"person.fullName": "张三"},
    )

    assert driver.commands[0].value == "张三"


@pytest.mark.asyncio
async def test_executor_does_not_touch_browser_when_policy_blocks_action() -> None:
    executor, driver, _ = make_executor()

    with pytest.raises(PolicyViolationError, match="origin"):
        await executor.execute(
            ActionProposal(
                action=ActionType.CLICK,
                element_id="ad-button",
                confidence=0.99,
            ),
            current_url="https://ads.example.net",
            record_values={},
        )

    assert driver.commands == []


@pytest.mark.asyncio
async def test_submit_waits_for_explicit_human_approval() -> None:
    executor, driver, _ = make_executor()
    proposal = ActionProposal(
        action=ActionType.SUBMIT,
        element_id="save",
        confidence=0.99,
    )

    with pytest.raises(HumanApprovalRequiredError):
        await executor.execute(
            proposal,
            current_url="https://target.example.com/profile",
            record_values={},
        )
    assert driver.commands == []

    await executor.execute(
        proposal,
        current_url="https://target.example.com/profile",
        record_values={},
        human_approved=True,
    )
    assert driver.commands[0].action is ActionType.SUBMIT


@pytest.mark.asyncio
async def test_missing_record_reference_returns_a_generic_policy_error() -> None:
    executor, _, _ = make_executor()

    with pytest.raises(PolicyViolationError, match="unavailable"):
        await executor.execute(
            ActionProposal(
                action=ActionType.FILL,
                element_id="name",
                value_ref="record://person.fullName",
                confidence=0.99,
            ),
            current_url="https://target.example.com/profile",
            record_values={},
        )
