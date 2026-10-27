"""Chequeo e instalación de actualizaciones.

Orden de las verificaciones (cualquier falla corta antes de instalar nada):

1. Hay clave pública configurada y una plataforma conocida.
2. El último release *publicado* (los borradores no aparecen) trae
   ``manifest.json`` y ``manifest.json.sig``.
3. La firma del manifiesto es válida con la clave embebida.
4. El manifiesto es para esta plataforma, su versión coincide con el tag del
   release y es estrictamente mayor que la instalada (no se instalan versiones
   iguales ni más viejas).
5. El archivo descargado tiene exactamente el tamaño y el sha256 firmados.

Sin internet (o con GitHub caído) el resultado es ``offline`` y la app sigue
igual. Solo se instala si corre como ejecutable empaquetado; desde el código
fuente solo se informa que hay una versión nueva.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from nacl.signing import VerifyKey

from coclody import __version__
from coclody.paths import executable_path
from coclody.updater.http import DownloadError, Http, NetworkError, UrllibHttp
from coclody.updater.installer import PART_SUFFIX, InstallError, replace_executable
from coclody.updater.manifest import (
    MAX_MANIFEST_BYTES,
    MAX_SIGNATURE_BYTES,
    ManifestError,
    current_platform,
    parse_version,
    verify_signed,
)
from coclody.updater.pubkey import release_verify_key

log = logging.getLogger(__name__)

MANIFEST_NAME = "manifest.json"
SIGNATURE_NAME = "manifest.json.sig"
MAX_RELEASE_JSON_BYTES = 1024 * 1024
_UNSET = object()


@dataclass(frozen=True)
class UpdateResult:
    status: str  # disabled, offline, up_to_date, available, installed, error
    message: str
    version: str | None = None


class Updater:
    def __init__(
        self,
        github_repo: str,
        updates_dir: Path,
        *,
        current_version: str = __version__,
        http: Http | None = None,
        verify_key: VerifyKey | None | object = _UNSET,
        executable: Path | None | object = _UNSET,
        platform_name: str | None | object = _UNSET,
    ):
        self.repo = github_repo
        self.updates_dir = updates_dir
        self.current = parse_version(current_version)
        self.http = http or UrllibHttp()
        self.key = release_verify_key() if verify_key is _UNSET else verify_key
        self.executable = executable_path() if executable is _UNSET else executable
        self.platform = current_platform() if platform_name is _UNSET else platform_name

    def check(self, install: bool = True) -> UpdateResult:
        try:
            return self._check(install)
        except NetworkError as exc:
            log.info("Sin conexión para buscar actualizaciones: %s", exc)
            return UpdateResult("offline", "Sin conexión; se reintenta más tarde.")
        except (DownloadError, ManifestError, InstallError) as exc:
            log.warning("Actualización rechazada: %s", exc)
            return UpdateResult("error", str(exc))

    def _check(self, install: bool) -> UpdateResult:
        if self.key is None:
            return UpdateResult("disabled", "No hay clave pública de releases configurada; no se verifica ni instala nada.")
        if self.platform is None:
            return UpdateResult("disabled", "Plataforma sin ejecutables publicados.")

        release = self._latest_release()
        assets = _assets(release)
        if MANIFEST_NAME not in assets or SIGNATURE_NAME not in assets:
            raise ManifestError("el último release no tiene manifiesto firmado")
        manifest_bytes = self.http.get(assets[MANIFEST_NAME]["url"], MAX_MANIFEST_BYTES)
        signature = self.http.get(assets[SIGNATURE_NAME]["url"], MAX_SIGNATURE_BYTES)
        manifest = verify_signed(manifest_bytes, signature, self.key)

        if release.get("tag_name") != f"v{manifest.version}":
            raise ManifestError(f"el manifiesto (v{manifest.version}) no corresponde al release {release.get('tag_name')!r}")
        if manifest.platform != self.platform:
            return UpdateResult("up_to_date", f"El último release no tiene versión para {self.platform}.")
        if manifest.version_tuple <= self.current:
            return UpdateResult("up_to_date", "Ya tenés la última versión.", manifest.version)
        if not install or self.executable is None:
            return UpdateResult("available", f"Hay una versión nueva: {manifest.version}.", manifest.version)

        asset = assets.get(manifest.asset)
        if asset is None:
            raise ManifestError(f"el release no tiene el archivo {manifest.asset!r}")
        if asset["size"] != manifest.size:
            raise ManifestError("el tamaño del archivo publicado no coincide con el manifiesto")

        self.updates_dir.mkdir(parents=True, exist_ok=True)
        staged = self.updates_dir / f"{manifest.asset}-{manifest.version}{PART_SUFFIX}"
        try:
            digest = self.http.download(asset["url"], staged, manifest.size)
            if staged.stat().st_size != manifest.size or digest != manifest.sha256:
                raise DownloadError("el archivo descargado no coincide con el manifiesto firmado")
            replace_executable(self.executable, staged, manifest.sha256, manifest.size)
        finally:
            staged.unlink(missing_ok=True)
        log.warning("Instalada la versión %s en %s", manifest.version, self.executable)
        return UpdateResult(
            "installed", f"Se instaló la versión {manifest.version}; se aplica al reiniciar CoClody.", manifest.version
        )

    def _latest_release(self) -> dict:
        url = f"https://api.github.com/repos/{self.repo}/releases/latest"
        raw = self.http.get(url, MAX_RELEASE_JSON_BYTES, accept="application/vnd.github+json")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise DownloadError("respuesta inválida de GitHub") from None
        if not isinstance(data, dict):
            raise DownloadError("respuesta inválida de GitHub")
        return data


def _assets(release: dict) -> dict[str, dict]:
    out = {}
    for asset in release.get("assets") or []:
        if not isinstance(asset, dict):
            continue
        name, url, size = asset.get("name"), asset.get("browser_download_url"), asset.get("size")
        if isinstance(name, str) and isinstance(url, str) and type(size) is int and name not in out:
            out[name] = {"url": url, "size": size}
    return out
