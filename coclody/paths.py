"""Rutas de datos, configuración y logs según las convenciones de cada SO.

Es el único módulo que decide dónde viven los archivos del usuario. No depende
de ``__file__``, así que da lo mismo correr desde fuente o desde el ejecutable
de PyInstaller (donde ``__file__`` apunta a una carpeta temporal).

- macOS:   ~/Library/Application Support/CoClody   (logs en ~/Library/Logs/CoClody)
- Windows: %LOCALAPPDATA%\\CoClody                  (logs en ...\\CoClody\\logs)
- Linux:   $XDG_DATA_HOME/coclody, $XDG_CONFIG_HOME/coclody, $XDG_STATE_HOME/coclody
           (no es plataforma objetivo; existe para que nada explote en CI)
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

APP_NAME = "CoClody"


@dataclass(frozen=True)
class AppPaths:
    data_dir: Path
    config_dir: Path
    logs_dir: Path

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def db_file(self) -> Path:
        return self.data_dir / "coclody.db"

    @property
    def app_log(self) -> Path:
        return self.logs_dir / "coclody.log"

    @property
    def security_log(self) -> Path:
        return self.logs_dir / "security.log"

    @property
    def updates_dir(self) -> Path:
        return self.data_dir / "updates"

    @classmethod
    def under(cls, root: Path) -> AppPaths:
        """Todo bajo una misma raíz (tests, o instalaciones portables a futuro)."""
        return cls(data_dir=root, config_dir=root, logs_dir=root / "logs")

    def all_dirs(self) -> tuple[Path, ...]:
        return tuple(dict.fromkeys((self.data_dir, self.config_dir, self.logs_dir, self.updates_dir)))

    def ensure(self) -> None:
        """Crea las carpetas. En POSIX quedan con permisos 0700 (solo el usuario)."""
        for directory in self.all_dirs():
            directory.mkdir(parents=True, exist_ok=True)
            if os.name == "posix":
                directory.chmod(0o700)


def default_paths(
    platform: str | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> AppPaths:
    """Rutas por defecto para la plataforma. Los parámetros existen para testear."""
    platform = sys.platform if platform is None else platform
    env = os.environ if env is None else env
    home = Path.home() if home is None else home

    if platform == "darwin":
        data = home / "Library" / "Application Support" / APP_NAME
        return AppPaths(data_dir=data, config_dir=data, logs_dir=home / "Library" / "Logs" / APP_NAME)

    if platform == "win32":
        # LOCALAPPDATA y no APPDATA (Roaming): nada de esto debe sincronizarse a otras máquinas.
        base = _env_dir(env, "LOCALAPPDATA", windows=True) or home / "AppData" / "Local"
        data = base / APP_NAME
        return AppPaths(data_dir=data, config_dir=data, logs_dir=data / "logs")

    name = APP_NAME.lower()
    return AppPaths(
        data_dir=(_env_dir(env, "XDG_DATA_HOME") or home / ".local" / "share") / name,
        config_dir=(_env_dir(env, "XDG_CONFIG_HOME") or home / ".config") / name,
        logs_dir=(_env_dir(env, "XDG_STATE_HOME") or home / ".local" / "state") / name,
    )


def _env_dir(env: Mapping[str, str], key: str, windows: bool = False) -> Path | None:
    # Solo se aceptan rutas absolutas: una relativa dependería del directorio actual.
    value = env.get(key, "").strip()
    flavor = PureWindowsPath if windows else PurePosixPath
    if value and flavor(value).is_absolute():
        return Path(value)
    return None


def is_frozen() -> bool:
    """True si corre empaquetado con PyInstaller."""
    return bool(getattr(sys, "frozen", False)) and hasattr(sys, "_MEIPASS")


def executable_path() -> Path | None:
    """Ruta del ejecutable instalado (lo que reemplaza el updater), o None desde fuente."""
    return Path(sys.executable).resolve() if is_frozen() else None
