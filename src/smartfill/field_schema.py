"""Validated, task-scoped field definitions for portable semantic form mapping."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

FIELD_KEY_PATTERN = re.compile(r"^[a-z][A-Za-z0-9]*(?:\.[A-Za-z][A-Za-z0-9]*)+$")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


class FieldInputKind(StrEnum):
    """Supported high-level field kinds used as portable HTML hints."""

    TEXT = "text"
    PASSWORD = "password"
    EMAIL = "email"
    TEL = "tel"
    SELECT = "select"


class FieldDefinition(BaseModel):
    """Describe one submitted value without binding it to a page selector."""

    model_config = ConfigDict(frozen=True)

    key: str = Field(min_length=3, max_length=100)
    display_name: str = Field(min_length=1, max_length=100)
    aliases: list[str] = Field(min_length=1, max_length=30)
    input_kind: FieldInputKind = FieldInputKind.TEXT
    sensitive: bool = False
    source_field: str | None = Field(default=None, min_length=3, max_length=100)
    autocomplete_hints: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("key", "source_field")
    @classmethod
    def validate_key(cls, value: str | None) -> str | None:
        if value is not None and FIELD_KEY_PATTERN.fullmatch(value) is None:
            raise ValueError("Field key must use a dotted alphanumeric identifier")
        return value

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or _CONTROL_CHARACTERS.search(normalized):
            raise ValueError("Field display name contains unsupported characters")
        return normalized

    @field_validator("aliases")
    @classmethod
    def validate_aliases(cls, values: list[str]) -> list[str]:
        normalized = cls._normalize_terms(values)
        if not normalized:
            raise ValueError("At least one field alias is required")
        return normalized

    @field_validator("autocomplete_hints")
    @classmethod
    def validate_autocomplete_hints(cls, values: list[str]) -> list[str]:
        return cls._normalize_terms(values)

    @staticmethod
    def _normalize_terms(values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            term = value.strip()
            if not term or len(term) > 100 or _CONTROL_CHARACTERS.search(term):
                raise ValueError("Field semantic term is invalid")
            folded = term.casefold()
            if folded not in seen:
                normalized.append(term)
                seen.add(folded)
        return normalized

    @model_validator(mode="after")
    def protect_password_fields(self) -> FieldDefinition:
        if self.input_kind is FieldInputKind.PASSWORD and not self.sensitive:
            raise ValueError("Password fields must be sensitive")
        return self

    @property
    def semantic_aliases(self) -> tuple[str, ...]:
        """Return aliases including the operator-facing field name."""

        return tuple(dict.fromkeys([*self.aliases, self.display_name]))


_DEFAULT_DEFINITIONS = {
    definition.key: definition
    for definition in [
        FieldDefinition(
            key="account.username",
            display_name="用户名",
            aliases=["用户名", "账号", "登录名", "username", "user", "login"],
            autocomplete_hints=["username"],
        ),
        FieldDefinition(
            key="account.email",
            display_name="账号邮箱",
            aliases=["账号邮箱", "注册邮箱", "email", "email address"],
            input_kind=FieldInputKind.EMAIL,
        ),
        FieldDefinition(
            key="account.password",
            display_name="密码",
            aliases=["密码", "password", "passwd", "pwd"],
            input_kind=FieldInputKind.PASSWORD,
            sensitive=True,
            autocomplete_hints=["current-password", "new-password"],
        ),
        FieldDefinition(
            key="account.passwordConfirmation",
            display_name="确认密码",
            aliases=["确认密码", "再次输入密码", "confirm password", "password confirmation"],
            input_kind=FieldInputKind.PASSWORD,
            sensitive=True,
            source_field="account.password",
            autocomplete_hints=["new-password"],
        ),
        FieldDefinition(
            key="person.fullName",
            display_name="姓名",
            aliases=["姓名", "真实姓名", "名字", "fullname", "name"],
            autocomplete_hints=["name"],
        ),
        FieldDefinition(
            key="person.gender",
            display_name="性别",
            aliases=["性别", "gender", "sex"],
            input_kind=FieldInputKind.SELECT,
        ),
        FieldDefinition(
            key="person.idType",
            display_name="证件类型",
            aliases=["证件类型", "证件种类", "idtype", "documenttype"],
            input_kind=FieldInputKind.SELECT,
        ),
        FieldDefinition(
            key="person.idNumber",
            display_name="身份证号",
            aliases=["身份证", "证件号码", "证件号", "idnumber", "idcard"],
            sensitive=True,
        ),
        FieldDefinition(
            key="person.phone",
            display_name="手机号",
            aliases=["手机号", "手机号码", "联系电话", "phone", "mobile", "tel"],
            input_kind=FieldInputKind.TEL,
            sensitive=True,
            autocomplete_hints=["tel", "tel-national"],
        ),
        FieldDefinition(
            key="person.email",
            display_name="邮箱",
            aliases=["邮箱", "电子邮件", "email", "mail"],
            input_kind=FieldInputKind.EMAIL,
            autocomplete_hints=["email"],
        ),
        FieldDefinition(
            key="person.address",
            display_name="地址",
            aliases=["地址", "联系地址", "居住地址", "address"],
            autocomplete_hints=["street-address", "address-line1"],
        ),
    ]
}


def default_field_definition(key: str) -> FieldDefinition:
    """Return a built-in definition for legacy payloads."""

    try:
        return _DEFAULT_DEFINITIONS[key]
    except KeyError as error:
        raise ValueError(f"Unsupported canonical field without a definition: {key}") from error


def default_field_definitions(keys: Iterable[str]) -> list[FieldDefinition]:
    return [default_field_definition(key) for key in keys]


def discoverable_field_definitions() -> list[FieldDefinition]:
    """Return built-in semantic definitions available during page discovery."""

    return list(_DEFAULT_DEFINITIONS.values())


def resolve_field_definitions(
    fields: Mapping[str, str],
    definitions: list[FieldDefinition],
) -> tuple[dict[str, str], list[FieldDefinition]]:
    """Validate schema coverage and resolve derived fields in definition order."""

    selected = definitions or default_field_definitions(fields)
    by_key: dict[str, FieldDefinition] = {}
    for definition in selected:
        if definition.key in by_key:
            raise ValueError(f"Duplicate field definition: {definition.key}")
        by_key[definition.key] = definition

    conflicting_derived = {
        definition.key
        for definition in selected
        if definition.source_field is not None and definition.key in fields
    }
    if conflicting_derived:
        raise ValueError(
            f"Derived fields cannot also submit a value: {sorted(conflicting_derived)}"
        )

    undefined = set(fields) - by_key.keys()
    if undefined:
        raise ValueError(f"Every submitted field requires a definition: {sorted(undefined)}")
    unbound = {
        definition.key
        for definition in selected
        if definition.key not in fields and definition.source_field is None
    }
    if unbound:
        raise ValueError(
            f"Every field definition requires a value or source field: {sorted(unbound)}"
        )

    resolved: dict[str, str] = {}
    resolving: set[str] = set()

    def resolve(key: str) -> str:
        if key in resolved:
            return resolved[key]
        if key in resolving:
            raise ValueError("Field source references cannot contain a cycle")
        try:
            definition = by_key[key]
        except KeyError as error:
            raise ValueError(f"Unknown source field: {key}") from error
        resolving.add(key)
        if key in fields:
            value = fields[key]
        elif definition.source_field is not None:
            source_definition = by_key.get(definition.source_field)
            if source_definition is None:
                raise ValueError(f"Unknown source field: {definition.source_field}")
            if source_definition.sensitive and not definition.sensitive:
                raise ValueError("A field derived from sensitive data must also be sensitive")
            value = resolve(definition.source_field)
        else:  # pragma: no cover - guarded by the unbound check above
            raise ValueError(f"Field definition has no value: {key}")
        resolving.remove(key)
        if not value or len(value) > 4_096:
            raise ValueError(f"Invalid value for field: {key}")
        resolved[key] = value
        return value

    ordered_values = {definition.key: resolve(definition.key) for definition in selected}
    return ordered_values, selected
