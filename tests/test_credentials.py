from __future__ import annotations

import os
import sys

import pytest
from keyring.backend import KeyringBackend
from keyring.errors import KeyringError, PasswordDeleteError

from coclody.credentials import (
    SERVICE,
    MissingSecretError,
    SecretError,
    SecretName,
    SecretStore,
    UnsupportedPlatformError,
    platform_backend,
)

TOKEN = "123456789:AAH" + "x" * 32
TG = SecretName.TELEGRAM_BOT_TOKEN


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        super().__init__()
        self.items: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self.items.get((service, username))

    def set_password(self, service, username, password):
        self.items[(service, username)] = password

    def delete_password(self, service, username):
        if self.items.pop((service, username), None) is None:
            raise PasswordDeleteError("no existe")


class BrokenKeyring(MemoryKeyring):
    def get_password(self, service, username):
        raise KeyringError(f"falló leyendo {TOKEN}")

    def set_password(self, service, username, password):
        raise KeyringError(f"falló guardando {password}")


@pytest.fixture
def backend():
    return MemoryKeyring()


@pytest.fixture
def store(backend):
    return SecretStore(backend=backend)


def test_backend_is_fixed_per_platform():
    assert type(platform_backend("darwin")).__module__ == "keyring.backends.macOS"
    assert type(platform_backend("darwin")).__name__ == "Keyring"
    assert type(platform_backend("win32")).__name__ == "WinVaultKeyring"
    with pytest.raises(UnsupportedPlatformError):
        platform_backend("linux")


def test_default_store_uses_platform_backend():
    if sys.platform not in ("darwin", "win32"):
        pytest.skip("solo macOS y Windows")
    assert type(SecretStore()._backend) is type(platform_backend())


def test_roundtrip(store, backend):
    assert store.get(TG) is None
    store.set(TG, TOKEN)
    assert backend.items == {(SERVICE, "telegram_bot_token"): TOKEN}
    assert store.require(TG) == TOKEN
    assert store.delete(TG) is True
    assert store.delete(TG) is False
    assert store.get(TG) is None


def test_require_missing(store):
    with pytest.raises(MissingSecretError, match="coclody setup"):
        store.require(TG)


@pytest.mark.parametrize(
    "value",
    ["", "abc", "123456789", "123456789:short", f"{TOKEN}\n", f" {TOKEN}", "abc:" + "x" * 35, None, 123],
)
def test_rejects_malformed_token(store, backend, value):
    with pytest.raises(SecretError) as exc:
        store.set(TG, value)
    assert backend.items == {}
    assert TOKEN not in str(exc.value)


def test_backend_errors_do_not_leak_values():
    store = SecretStore(backend=BrokenKeyring())
    with pytest.raises(SecretError) as exc:
        store.get(TG)
    assert TOKEN not in str(exc.value) and exc.value.__cause__ is None
    with pytest.raises(SecretError) as exc:
        store.set(TG, TOKEN)
    assert TOKEN not in str(exc.value) and exc.value.__cause__ is None


def test_repr_has_no_values(store):
    store.set(TG, TOKEN)
    assert TOKEN not in repr(store)


@pytest.mark.skipif(os.environ.get("COCLODY_TEST_REAL_KEYRING") != "1", reason="opt-in: usa el llavero real")
def test_real_platform_keyring():
    store = SecretStore(service="coclody-test")
    try:
        store.set(TG, TOKEN)
        assert store.require(TG) == TOKEN
    finally:
        store.delete(TG)
    assert store.get(TG) is None
