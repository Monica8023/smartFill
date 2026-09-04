from smartfill.browser_worker import DomElement, SemanticFieldMapper
from smartfill.field_schema import FieldDefinition, FieldInputKind


def test_mapper_understands_chinese_and_html_semantics_without_page_specific_selectors() -> None:
    elements = [
        DomElement(
            element_id="sf-1",
            tag="input",
            input_type="text",
            label="用户姓名",
            name="display_name",
        ),
        DomElement(
            element_id="sf-2",
            tag="input",
            input_type="email",
            label="联系邮箱",
            autocomplete="email",
        ),
        DomElement(
            element_id="sf-3",
            tag="input",
            input_type="password",
            label="登录密码",
            autocomplete="current-password",
        ),
    ]

    matches = SemanticFieldMapper().map_fields(
        elements,
        ["person.fullName", "person.email", "account.password"],
    )

    assert {match.canonical_field: match.element_id for match in matches} == {
        "person.fullName": "sf-1",
        "person.email": "sf-2",
        "account.password": "sf-3",
    }
    assert all(match.confidence >= 0.85 for match in matches)


def test_mapper_does_not_guess_when_two_elements_have_the_same_score() -> None:
    elements = [
        DomElement(element_id="sf-1", tag="input", input_type="text", label="姓名"),
        DomElement(element_id="sf-2", tag="input", input_type="text", label="姓名"),
    ]

    matches = SemanticFieldMapper().map_fields(elements, ["person.fullName"])

    assert matches == []


def test_one_element_cannot_be_assigned_to_multiple_fields() -> None:
    elements = [
        DomElement(
            element_id="sf-1",
            tag="input",
            input_type="text",
            label="用户名或姓名",
            name="username",
        )
    ]

    matches = SemanticFieldMapper().map_fields(
        elements,
        ["account.username", "person.fullName"],
    )

    assert len(matches) == 1


def test_mapper_uses_job_supplied_dynamic_field_definitions() -> None:
    elements = [
        DomElement(
            element_id="sf-first-name",
            tag="input",
            input_type="text",
            label="First Name",
            name="customer.firstName",
        ),
        DomElement(
            element_id="sf-ssn",
            tag="input",
            input_type="text",
            label="SSN",
            name="customer.ssn",
        ),
    ]
    definitions = [
        FieldDefinition(
            key="person.firstName",
            display_name="名",
            aliases=["First Name", "Given Name", "名"],
        ),
        FieldDefinition(
            key="person.ssn",
            display_name="SSN",
            aliases=["SSN", "Social Security Number"],
            input_kind=FieldInputKind.TEXT,
            sensitive=True,
        ),
    ]

    matches = SemanticFieldMapper().map_fields(elements, definitions)

    assert {match.canonical_field: match.element_id for match in matches} == {
        "person.firstName": "sf-first-name",
        "person.ssn": "sf-ssn",
    }


def test_explicit_aliases_disambiguate_password_and_confirmation_fields() -> None:
    elements = [
        DomElement(
            element_id="sf-password",
            tag="input",
            input_type="password",
            label="Password",
        ),
        DomElement(
            element_id="sf-confirm",
            tag="input",
            input_type="password",
            label="Confirm Password",
        ),
    ]
    definitions = [
        FieldDefinition(
            key="account.password",
            display_name="密码",
            aliases=["Password"],
            input_kind=FieldInputKind.PASSWORD,
            sensitive=True,
        ),
        FieldDefinition(
            key="account.passwordConfirmation",
            display_name="确认密码",
            aliases=["Confirm Password", "Confirm"],
            input_kind=FieldInputKind.PASSWORD,
            sensitive=True,
            source_field="account.password",
        ),
    ]

    matches = SemanticFieldMapper().map_fields(elements, definitions)

    assert {match.canonical_field: match.element_id for match in matches} == {
        "account.password": "sf-password",
        "account.passwordConfirmation": "sf-confirm",
    }


def test_login_form_context_treats_an_email_input_as_the_login_identifier() -> None:
    elements = [
        DomElement(
            element_id="sf-email",
            tag="input",
            input_type="text",
            label="Email address",
            name="email",
            form_context="Login Sign in",
        ),
        DomElement(
            element_id="sf-password",
            tag="input",
            input_type="password",
            label="Password",
            name="password",
            form_context="Login Sign in",
        ),
    ]

    matches = SemanticFieldMapper().map_fields(
        elements,
        ["account.username", "account.password"],
    )

    assert {match.canonical_field: match.element_id for match in matches} == {
        "account.username": "sf-email",
        "account.password": "sf-password",
    }


def test_registration_context_discovers_account_email_and_confirmation_password() -> None:
    elements = [
        DomElement(
            element_id="sf-email",
            tag="input",
            input_type="email",
            label="Email address",
            name="email",
            form_context="Register Create an account",
        ),
        DomElement(
            element_id="sf-password",
            tag="input",
            input_type="password",
            label="Password",
            name="password",
            form_context="Register Create an account",
        ),
        DomElement(
            element_id="sf-confirm",
            tag="input",
            input_type="password",
            label="Confirm Password",
            name="confirmPassword",
            form_context="Register Create an account",
        ),
    ]
    mapper = SemanticFieldMapper()

    matches = mapper.map_fields(
        elements,
        [
            "account.email",
            "account.password",
            "account.passwordConfirmation",
        ],
    )

    assert {match.canonical_field: match.element_id for match in matches} == {
        "account.email": "sf-email",
        "account.password": "sf-password",
        "account.passwordConfirmation": "sf-confirm",
    }


def test_ascii_aliases_use_token_boundaries_instead_of_substrings() -> None:
    element = DomElement(
        element_id="sf-username",
        tag="input",
        input_type="text",
        label="Username",
        name="username",
    )

    matches = SemanticFieldMapper().map_fields([element], ["person.fullName"])

    assert matches == []
