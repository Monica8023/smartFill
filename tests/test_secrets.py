from smartfill.secrets import InMemorySecretStore, redact_value


def test_secret_store_returns_an_opaque_reference() -> None:
    store = InMemorySecretStore()

    reference = store.put("record-1/account.password", "P@ssw0rd!")

    assert reference.startswith("secret://")
    assert "P@ssw0rd!" not in reference
    assert store.resolve(reference) == "P@ssw0rd!"


def test_secret_store_rejects_unknown_reference() -> None:
    store = InMemorySecretStore()

    try:
        store.resolve("secret://missing")
    except KeyError as error:
        assert "missing" in str(error)
    else:
        raise AssertionError("unknown secret reference must be rejected")


def test_redaction_keeps_only_a_small_non_sensitive_hint() -> None:
    assert redact_value("110101199001011234") == "110************234"
    assert redact_value("13800138000") == "138*****000"
    assert redact_value("ab") == "**"
