"""Firma de releases de CoClody (Ed25519 con PyNaCl).

La clave privada vive solo en el llavero del sistema de quien firma (servicio
``coclody-release``) y nunca en el repo ni en GitHub. CI genera el manifiesto
sin firmar; la firma se hace acá, localmente.

Uso:
    python tools/sign_release.py keygen
        Genera la clave (una sola vez), la guarda en el llavero e imprime la
        clave pública para pegar en coclody/updater/pubkey.py.

    python tools/sign_release.py manifest <asset> --version X.Y.Z --platform windows-x64 [--out manifest.json]
        Genera manifest.json (lo usa CI; no necesita la clave).

    python tools/sign_release.py sign <manifest.json> <asset>
        Verifica que el asset coincide con el manifiesto, que la clave del
        llavero es la que la app tiene embebida, y escribe manifest.json.sig.

    python tools/sign_release.py verify <manifest.json> <manifest.json.sig>
        Verifica una firma con la clave pública embebida.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nacl.signing import SigningKey, VerifyKey  # noqa: E402

from coclody import __version__  # noqa: E402
from coclody.credentials import RELEASE_SERVICE, SecretName, SecretStore  # noqa: E402
from coclody.updater.manifest import ManifestError, parse_manifest, render_manifest, verify_signed  # noqa: E402
from coclody.updater.pubkey import release_verify_key  # noqa: E402

SEED = SecretName.RELEASE_SIGNING_SEED


class SigningError(Exception):
    pass


def keygen(store: SecretStore, out: Callable[[str], None] = print) -> str:
    """Genera y guarda la clave. Nunca reemplaza una existente. Devuelve la pública (base64)."""
    if store.get(SEED):
        raise SigningError(
            "Ya hay una clave de firma en el llavero. Para rotarla, borrala a mano primero: "
            "las versiones instaladas solo aceptan la clave pública que tienen embebida."
        )
    key = SigningKey.generate()
    store.set(SEED, base64.b64encode(key.encode()).decode("ascii"))
    public = base64.b64encode(key.verify_key.encode()).decode("ascii")
    out("Clave de firma generada y guardada en el llavero del sistema.")
    out("Pegá esta clave pública en coclody/updater/pubkey.py (PUBLIC_KEY_B64):")
    out(public)
    return public


def make_manifest(asset: Path, version: str, platform_name: str) -> bytes:
    if version != __version__:
        raise SigningError(f"la versión {version} no coincide con coclody.__version__ ({__version__})")
    return render_manifest(version, platform_name, asset.name, asset.stat().st_size, _sha256(asset))


def sign(manifest_path: Path, asset: Path, store: SecretStore, embedded: VerifyKey | None = None) -> Path:
    """Firma manifest.json y escribe manifest.json.sig al lado. Devuelve la ruta de la firma."""
    manifest_bytes = manifest_path.read_bytes()
    try:
        manifest = parse_manifest(manifest_bytes)
    except ManifestError as exc:
        raise SigningError(f"manifiesto inválido: {exc}") from None
    if asset.name != manifest.asset:
        raise SigningError(f"el manifiesto es para {manifest.asset!r}, no para {asset.name!r}")
    if asset.stat().st_size != manifest.size or _sha256(asset) != manifest.sha256:
        raise SigningError("el archivo no coincide con el tamaño/sha256 del manifiesto")

    embedded = release_verify_key() if embedded is None else embedded
    if embedded is None:
        raise SigningError("coclody/updater/pubkey.py no tiene clave pública: la app no podría verificar esta firma")
    seed = store.get(SEED)
    if not seed:
        raise SigningError("no hay clave de firma en el llavero; corré `keygen`")
    key = SigningKey(base64.b64decode(seed))
    if key.verify_key.encode() != embedded.encode():
        raise SigningError("la clave del llavero no es la que la app tiene embebida en pubkey.py")

    signature = base64.b64encode(key.sign(manifest_bytes).signature) + b"\n"
    verify_signed(manifest_bytes, signature, embedded)  # autoverificación
    sig_path = manifest_path.with_name(manifest_path.name + ".sig")
    sig_path.write_bytes(signature)
    return sig_path


def verify(manifest_path: Path, sig_path: Path, embedded: VerifyKey | None = None) -> str:
    embedded = release_verify_key() if embedded is None else embedded
    if embedded is None:
        raise SigningError("coclody/updater/pubkey.py no tiene clave pública")
    try:
        manifest = verify_signed(manifest_path.read_bytes(), sig_path.read_bytes(), embedded)
    except ManifestError as exc:
        raise SigningError(str(exc)) from None
    return manifest.version


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Firma de releases de CoClody.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("keygen")
    m = sub.add_parser("manifest")
    m.add_argument("asset", type=Path)
    m.add_argument("--version", required=True)
    m.add_argument("--platform", required=True)
    m.add_argument("--out", type=Path)
    s = sub.add_parser("sign")
    s.add_argument("manifest", type=Path)
    s.add_argument("asset", type=Path)
    v = sub.add_parser("verify")
    v.add_argument("manifest", type=Path)
    v.add_argument("signature", type=Path)
    args = parser.parse_args(argv)

    try:
        if args.command == "keygen":
            keygen(SecretStore(service=RELEASE_SERVICE))
        elif args.command == "manifest":
            data = make_manifest(args.asset, args.version, args.platform)
            if args.out:
                args.out.write_bytes(data)
            else:
                sys.stdout.write(data.decode("utf-8"))
        elif args.command == "sign":
            path = sign(args.manifest, args.asset, SecretStore(service=RELEASE_SERVICE))
            print(f"Firma escrita en {path}")
        else:
            print(f"Firma válida (versión {verify(args.manifest, args.signature)}).")
    except (SigningError, ManifestError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
