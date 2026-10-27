from __future__ import annotations

import base64
import importlib.util
from pathlib import Path

import pytest
from nacl.signing import SigningKey

from coclody import __version__
from coclody.credentials import SecretName, SecretStore
from coclody.updater.manifest import parse_manifest
from tests.test_credentials import MemoryKeyring

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("sign_release", ROOT / "tools" / "sign_release.py")
sign_release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sign_release)

SEED = SecretName.RELEASE_SIGNING_SEED


@pytest.fixture
def store():
    return SecretStore(backend=MemoryKeyring(), service="coclody-release")


@pytest.fixture
def asset(tmp_path):
    path = tmp_path / "coclody.exe"
    path.write_bytes(b"MZ" + b"\x00" * 1000)
    return path


def embedded_from(store):
    seed = base64.b64decode(store.get(SEED))
    return SigningKey(seed).verify_key


def test_keygen_stores_key_and_prints_only_public(store):
    lines = []
    public = sign_release.keygen(store, out=lines.append)
    seed = store.get(SEED)
    assert seed and len(base64.b64decode(seed)) == 32
    assert public in lines and seed not in "\n".join(lines)
    assert base64.b64decode(public) == embedded_from(store).encode()


def test_keygen_never_replaces(store):
    sign_release.keygen(store, out=lambda _: None)
    before = store.get(SEED)
    with pytest.raises(sign_release.SigningError, match="Ya hay una clave"):
        sign_release.keygen(store, out=lambda _: None)
    assert store.get(SEED) == before


def test_manifest_matches_asset(asset):
    raw = sign_release.make_manifest(asset, __version__, "windows-x64")
    m = parse_manifest(raw)
    assert (m.asset, m.size, m.version) == ("coclody.exe", asset.stat().st_size, __version__)


def test_manifest_version_must_match_package(asset):
    with pytest.raises(sign_release.SigningError, match="no coincide"):
        sign_release.make_manifest(asset, "9.9.9", "windows-x64")


def write_manifest(asset):
    path = asset.with_name("manifest.json")
    path.write_bytes(sign_release.make_manifest(asset, __version__, "windows-x64"))
    return path


def test_sign_and_verify(store, asset):
    sign_release.keygen(store, out=lambda _: None)
    embedded = embedded_from(store)
    manifest = write_manifest(asset)
    sig = sign_release.sign(manifest, asset, store, embedded)
    assert sig.name == "manifest.json.sig"
    assert sign_release.verify(manifest, sig, embedded) == __version__
    assert store.get(SEED) not in sig.read_text()


def test_verify_detects_tampering(store, asset):
    sign_release.keygen(store, out=lambda _: None)
    embedded = embedded_from(store)
    manifest = write_manifest(asset)
    sig = sign_release.sign(manifest, asset, store, embedded)
    manifest.write_bytes(manifest.read_bytes().replace(b"windows-x64", b"macos-arm64"))
    with pytest.raises(sign_release.SigningError, match="firma"):
        sign_release.verify(manifest, sig, embedded)


def test_sign_refuses_key_not_embedded_in_app(store, asset):
    sign_release.keygen(store, out=lambda _: None)
    manifest = write_manifest(asset)
    with pytest.raises(sign_release.SigningError, match="no es la que la app tiene embebida"):
        sign_release.sign(manifest, asset, store, SigningKey.generate().verify_key)
    assert not manifest.with_name("manifest.json.sig").exists()


def test_sign_refuses_without_embedded_key(store, asset, monkeypatch):
    monkeypatch.setattr(sign_release, "release_verify_key", lambda: None)  # pubkey.py vacío
    sign_release.keygen(store, out=lambda _: None)
    manifest = write_manifest(asset)
    with pytest.raises(sign_release.SigningError, match="pubkey.py"):
        sign_release.sign(manifest, asset, store)


def test_sign_refuses_mismatching_asset(store, asset):
    sign_release.keygen(store, out=lambda _: None)
    embedded = embedded_from(store)
    manifest = write_manifest(asset)
    asset.write_bytes(b"otro contenido")
    with pytest.raises(sign_release.SigningError, match="no coincide"):
        sign_release.sign(manifest, asset, store, embedded)
    renamed = asset.with_name("otro.exe")
    renamed.write_bytes(b"x")
    with pytest.raises(sign_release.SigningError, match="es para"):
        sign_release.sign(manifest, renamed, store, embedded)


def test_sign_without_key_in_keyring(store, asset):
    manifest = write_manifest(asset)
    with pytest.raises(sign_release.SigningError, match="keygen"):
        sign_release.sign(manifest, asset, store, SigningKey.generate().verify_key)


def test_cli_manifest_subcommand(asset, tmp_path, capsys):
    out = tmp_path / "m.json"
    assert sign_release.main(["manifest", str(asset), "--version", __version__, "--platform", "windows-x64", "--out", str(out)]) == 0
    assert parse_manifest(out.read_bytes()).asset == "coclody.exe"
    assert sign_release.main(["manifest", str(asset), "--version", "0.0.0", "--platform", "windows-x64"]) == 1
    assert "no coincide" in capsys.readouterr().err
