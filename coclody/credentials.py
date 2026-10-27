"""Acceso a secretos en el llavero del sistema vía keyring.

El backend se fija explícitamente según la plataforma (Keychain en macOS,
Credential Manager en Windows) en lugar de dejar que keyring lo descubra:
el descubrimiento usa entry points que PyInstaller puede no empaquetar, y
además podría elegir un backend inesperado (p. ej. uno en texto plano).

Los valores nunca se loguean ni aparecen en mensajes de error.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from enum import Enum

from keyring.backend import KeyringBackend
from keyring.errors import KeyringError, PasswordDeleteError

SERVICE = "coclody"


class SecretName(Enum):
    TELEGRAM_BOT_TOKEN = "telegram_bot_token"


# Formato de token de bot de Telegram: "<id numérico>:<35 caracteres aprox.>"
_VALIDATORS: dict[SecretName, Callable[[str], bool]] = {
    SecretName.TELEGRAM_BOT_TOKEN: re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{30,64}$").fullmatch,
}


class SecretError(Exception):
    pass


class MissingSecretError(SecretError):
    pass


class UnsupportedPlatformError(SecretError):
    pass


def platform_backend(platform: str | None = None) -> KeyringBackend:
    """Backend de keyring fijo para la plataforma."""
    platform = sys.platform if platform is None else platform
    if platform == "darwin":
        from keyring.backends.macOS import Keyring

        return Keyring()
    if platform == "win32":
        from keyring.backends.Windows import WinVaultKeyring

        return WinVaultKeyring()
    raise UnsupportedPlatformError(f"Plataforma no soportada para el llavero: {platform}")


class SecretStore:
    def __init__(self, backend: KeyringBackend | None = None, service: str = SERVICE):
        self._backend = platform_backend() if backend is None else backend
        self._service = service

    def get(self, name: SecretName) -> str | None:
        try:
            return self._backend.get_password(self._service, name.value)
        except KeyringError as exc:
            raise SecretError(f"No se pudo leer {name.value} del llavero: {type(exc).__name__}") from None

    def require(self, name: SecretName) -> str:
        value = self.get(name)
        if not value:
            raise MissingSecretError(f"Falta {name.value} en el llavero; corré `coclody setup`.")
        return value

    def set(self, name: SecretName, value: str) -> None:
        if not isinstance(value, str) or not _VALIDATORS[name](value):
            raise SecretError(f"El valor para {name.value} no tiene el formato esperado.")
        try:
            self._backend.set_password(self._service, name.value, value)
        except KeyringError as exc:
            raise SecretError(f"No se pudo guardar {name.value} en el llavero: {type(exc).__name__}") from None

    def delete(self, name: SecretName) -> bool:
        """Borra el secreto. Devuelve False si no existía."""
        try:
            self._backend.delete_password(self._service, name.value)
        except PasswordDeleteError:
            return False
        except KeyringError as exc:
            raise SecretError(f"No se pudo borrar {name.value} del llavero: {type(exc).__name__}") from None
        return True

    def __repr__(self) -> str:
        return f"SecretStore(service={self._service!r}, backend={type(self._backend).__name__})"
