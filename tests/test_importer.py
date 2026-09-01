import io

import pytest
from openpyxl import Workbook

from smartfill.importing import ImportValidationError, ProfileImporter
from smartfill.secrets import InMemorySecretStore

MAPPING = {
    "username": "account.username",
    "password": "account.password",
    "full_name": "person.fullName",
    "gender": "person.gender",
    "id_number": "person.idNumber",
    "phone": "person.phone",
    "email": "person.email",
}


def test_csv_import_normalizes_fields_and_extracts_secrets() -> None:
    content = (
        "username,password,full_name,gender,id_number,phone,email\n"
        "zhangsan,P@ssw0rd!,张三,男,110101199001011234,13800138000,zhangsan@example.com\n"
    ).encode()
    importer = ProfileImporter(secret_store=InMemorySecretStore())

    preview = importer.preview("people.csv", content, MAPPING)

    assert preview.total_rows == 1
    record = preview.records[0]
    assert record.values["person.gender"] == "male"
    assert record.secret_refs["account.password"].startswith("secret://")
    assert record.secret_refs["person.idNumber"].startswith("secret://")
    assert preview.rows[0]["password"] == "********"
    assert preview.rows[0]["id_number"] == "110************234"


def test_xlsx_import_is_supported() -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["username", "full_name", "gender"])
    sheet.append(["lisi", "李四", "female"])
    buffer = io.BytesIO()
    workbook.save(buffer)

    preview = ProfileImporter(InMemorySecretStore()).preview(
        "people.xlsx",
        buffer.getvalue(),
        {
            "username": "account.username",
            "full_name": "person.fullName",
            "gender": "person.gender",
        },
    )

    assert preview.total_rows == 1
    assert preview.records[0].values["person.fullName"] == "李四"


def test_import_rejects_unapproved_file_types_and_large_uploads() -> None:
    importer = ProfileImporter(InMemorySecretStore(), max_bytes=10)

    with pytest.raises(ImportValidationError, match="CSV or XLSX"):
        importer.preview("people.exe", b"x", {})

    with pytest.raises(ImportValidationError, match="too large"):
        importer.preview("people.csv", b"01234567890", {})


def test_import_rejects_unknown_canonical_fields() -> None:
    importer = ProfileImporter(InMemorySecretStore())

    with pytest.raises(ImportValidationError, match="Unsupported canonical field"):
        importer.preview("people.csv", b"name\nAlice\n", {"name": "system.prompt"})


def test_preview_masks_known_sensitive_columns_even_when_unmapped() -> None:
    content = b"username,password,id_number\nalice,plain-secret,110101199001011234\n"

    preview = ProfileImporter(InMemorySecretStore()).preview(
        "people.csv",
        content,
        {"username": "account.username"},
    )

    assert preview.rows[0]["password"] == "********"
    assert preview.rows[0]["id_number"] == "110************234"


def test_import_accepts_workflow_scoped_custom_fields() -> None:
    importer = ProfileImporter(InMemorySecretStore())

    preview = importer.preview(
        "people.csv",
        b"employee_code\nSF-001\n",
        {"employee_code": "profile.employeeCode"},
        allowed_fields={"profile.employeeCode"},
    )

    assert preview.records[0].values == {"profile.employeeCode": "SF-001"}


def test_import_requires_every_workflow_field_mapping() -> None:
    importer = ProfileImporter(InMemorySecretStore())

    with pytest.raises(ImportValidationError, match="Missing field mappings"):
        importer.preview(
            "people.csv",
            b"username\nalice\n",
            {"username": "account.username"},
            allowed_fields={"account.username", "account.password"},
            required_fields={"account.username", "account.password"},
        )
