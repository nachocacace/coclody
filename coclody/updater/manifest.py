"""Manifiesto de versión y su firma.

Formato (JSON, firmado byte a byte tal cual está en el archivo):

    {"schema": 1, "app": "coclody", "version": "0.2.0", "platform": "windows-x64",
     "asset": "coclody.exe", "size": 16113024, "sha256": "<64 hex>"}

La firma es Ed25519 *detached* sobre los bytes exactos de manifest.json,
guardada en base64 en manifest.json.sig. Primero se verifica la firma y recién
después se interpreta el contenido: nada sin firmar llega a parsearse.
"""

from __future__ import annotations

import base64
import binascii
import json
import platform
import re
import sys
from dataclasses import dataclass

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

SCHEMA = 1
APP = "coclody"
MAX_MANIFEST_BYTES = 16 * 1024
MAX_SIGNATURE_BYTES = 1024
MAX_ASSET_BYTES = 200 * 1024 * 1024
PLATFORMS = frozenset({"windows-x64", "macos-arm64"})

_VERSION_RE = re.compile(r"(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})")
_ASSET_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_KEYS = {"schema", "app", "version", "platform", "asset", "size", "sha256"}


class ManifestError(Exception):
    pass


@dataclass(frozen=True)
class Manifest:
    version: str
    platform: str
    asset: str
    size: int
    sha256: str

    @property
    def version_tuple(self) -> tuple[int, int, int]:
        return parse_version(self.version)


def parse_version(value: str) -> tuple[int, int, int]:
    match = _VERSION_RE.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ManifestError(f"versión inválida: {value!r} (se espera X.Y.Z)")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def current_platform() -> str | None:
    machine = platform.machine().lower()
    if sys.platform == "win32" and machine in ("amd64", "x86_64"):
        return "windows-x64"
    if sys.platform == "darwin" and machine == "arm64":
        return "macos-arm64"
    return None


def verify_signed(manifest_bytes: bytes, signature_text: bytes, key: VerifyKey) -> Manifest:
    """Verifica la firma y después interpreta el manifiesto. Lanza ManifestError."""
    if len(manifest_bytes) > MAX_MANIFEST_BYTES:
        raise ManifestError("el manifiesto es demasiado grande")
    if len(signature_text) > MAX_SIGNATURE_BYTES:
        raise ManifestError("la firma es demasiado grande")
    try:
        signature = base64.b64decode(signature_text.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise ManifestError("la firma no es base64 válido") from None
    if len(signature) != 64:
        raise ManifestError("la firma no tiene el largo de una firma Ed25519")
    try:
        key.verify(manifest_bytes, signature)
    except BadSignatureError:
        raise ManifestError("la firma del manifiesto no es válida") from None
    return parse_manifest(manifest_bytes)


def parse_manifest(manifest_bytes: bytes) -> Manifest:
    """Interpreta y valida el contenido. Solo para bytes ya verificados (o para firmarlos)."""
    if len(manifest_bytes) > MAX_MANIFEST_BYTES:
        raise ManifestError("el manifiesto es demasiado grande")
    try:
        data = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ManifestError("el manifiesto no es JSON válido") from None
    if not isinstance(data, dict) or set(data) != _KEYS:
        raise ManifestError(f"el manifiesto tiene que tener exactamente las claves {sorted(_KEYS)}")
    if data["schema"] != SCHEMA or type(data["schema"]) is not int:
        raise ManifestError(f"schema no soportado: {data['schema']!r}")
    if data["app"] != APP:
        raise ManifestError(f"el manifiesto es de otra aplicación: {data['app']!r}")
    parse_version(data["version"])
    if data["platform"] not in PLATFORMS:
        raise ManifestError(f"plataforma desconocida: {data['platform']!r}")
    if not isinstance(data["asset"], str) or not _ASSET_RE.fullmatch(data["asset"]):
        raise ManifestError(f"nombre de archivo inválido: {data['asset']!r}")
    if type(data["size"]) is not int or not 0 < data["size"] <= MAX_ASSET_BYTES:
        raise ManifestError(f"tamaño inválido: {data['size']!r}")
    if not isinstance(data["sha256"], str) or not _SHA256_RE.fullmatch(data["sha256"]):
        raise ManifestError("sha256 inválido (se esperan 64 caracteres hex en minúscula)")
    return Manifest(
        version=data["version"],
        platform=data["platform"],
        asset=data["asset"],
        size=data["size"],
        sha256=data["sha256"],
    )


def render_manifest(version: str, platform_name: str, asset: str, size: int, sha256: str) -> bytes:
    """Bytes del manifiesto (lo que se firma). Valida con las mismas reglas que al leerlo."""
    data = {
        "schema": SCHEMA,
        "app": APP,
        "version": version,
        "platform": platform_name,
        "asset": asset,
        "size": size,
        "sha256": sha256,
    }
    raw = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")
    parse_manifest(raw)
    return raw
