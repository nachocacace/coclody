from __future__ import annotations

import base64
import hashlib
import http.client
import io
import json
import os
import sys
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pytest
from nacl.signing import SigningKey

from coclody.updater import client as client_module
from coclody.updater import installer
from coclody.updater.client import MANIFEST_NAME, SIGNATURE_NAME, Updater
from coclody.updater.http import DownloadError, NetworkError, UrllibHttp, _HttpsOnlyRedirects, stream_to_file
from coclody.updater.manifest import (
    ManifestError,
    current_platform,
    parse_manifest,
    parse_version,
    render_manifest,
    verify_signed,
)
from coclody.updater.pubkey import release_verify_key

REPO = "nacho/coclody"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
BASE = "https://github.com/nacho/coclody/releases/download"
NEW_EXE = b"MZ-version-nueva-" * 4096
OLD_EXE = b"MZ-version-vieja"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sign(key: SigningKey, data: bytes) -> bytes:
    return base64.b64encode(key.sign(data).signature) + b"\n"


class CutResponse(io.BytesIO):
    """Respuesta que se corta después de ``cut`` bytes, como una conexión caída."""

    def __init__(self, data: bytes, cut: int):
        super().__init__(data)
        self.cut = cut

    def read(self, size=-1):
        if self.tell() >= self.cut:
            raise http.client.IncompleteRead(b"", len(self.getvalue()) - self.tell())
        return super().read(min(size, self.cut - self.tell()) if size and size > 0 else self.cut - self.tell())


class FakeHttp(UrllibHttp):
    """El UrllibHttp real con la red reemplazada por rutas en memoria."""

    def __init__(self, routes: dict):
        super().__init__()
        self.routes = routes
        self.requested: list[str] = []

    @contextmanager
    def _fake(self, url):
        self.requested.append(url)
        value = self.routes.get(url, DownloadError(f"HTTP 404 en {url}"))
        if isinstance(value, Exception):
            raise value
        yield value if hasattr(value, "read") else io.BytesIO(value)

    def _open(self, url, accept):
        if not url.lower().startswith("https://"):
            raise DownloadError(f"solo se admiten URLs HTTPS: {url}")
        return self._fake(url)


class Release:
    def __init__(self, key, version="0.2.0", platform="windows-x64", asset=NEW_EXE, tag=None):
        self.key = key
        self.manifest = render_manifest(version, platform, "coclody.exe", len(asset), sha(asset))
        self.signature = sign(key, self.manifest)
        self.asset = asset
        self.tag = tag or f"v{version}"
        self.asset_size = len(asset)

    def routes(self, **overrides):
        assets = [
            {"name": "coclody.exe", "browser_download_url": f"{BASE}/{self.tag}/coclody.exe", "size": self.asset_size},
            {"name": MANIFEST_NAME, "browser_download_url": f"{BASE}/{self.tag}/{MANIFEST_NAME}", "size": 200},
            {"name": SIGNATURE_NAME, "browser_download_url": f"{BASE}/{self.tag}/{SIGNATURE_NAME}", "size": 90},
        ]
        routes = {
            API: json.dumps({"tag_name": self.tag, "assets": assets}).encode(),
            f"{BASE}/{self.tag}/{MANIFEST_NAME}": self.manifest,
            f"{BASE}/{self.tag}/{SIGNATURE_NAME}": self.signature,
            f"{BASE}/{self.tag}/coclody.exe": self.asset,
        }
        routes.update(overrides)
        return routes

    def url(self, name):
        return f"{BASE}/{self.tag}/{name}"


@pytest.fixture
def key():
    return SigningKey.generate()


@pytest.fixture
def exe(tmp_path):
    path = tmp_path / "app" / "coclody.exe"
    path.parent.mkdir()
    path.write_bytes(OLD_EXE)
    return path


@pytest.fixture
def updates_dir(tmp_path):
    return tmp_path / "updates"


def make_updater(key, routes, exe, updates_dir, **kw):
    http_ = FakeHttp(routes)
    kw.setdefault("current_version", "0.1.0")
    kw.setdefault("platform_name", "windows-x64")
    up = Updater(REPO, updates_dir, http=http_, verify_key=key.verify_key, executable=exe, **kw)
    return up, http_


def assert_untouched(exe, updates_dir):
    assert exe.read_bytes() == OLD_EXE
    assert not exe.with_name("coclody.exe.old").exists()
    assert not exe.with_name("coclody.exe.new").exists()
    assert not updates_dir.exists() or list(updates_dir.iterdir()) == []


# --- camino feliz ----------------------------------------------------------------------


def test_installs_signed_newer_version(key, exe, updates_dir):
    release = Release(key)
    up, _ = make_updater(key, release.routes(), exe, updates_dir)
    result = up.check()
    assert result.status == "installed" and result.version == "0.2.0"
    assert exe.read_bytes() == NEW_EXE
    assert exe.with_name("coclody.exe.old").read_bytes() == OLD_EXE  # el proceso actual sigue desde acá
    assert list(updates_dir.iterdir()) == []
    if os.name == "posix":
        assert os.access(exe, os.X_OK)


def test_cleanup_on_next_start_removes_old(key, exe, updates_dir):
    up, _ = make_updater(key, Release(key).routes(), exe, updates_dir)
    up.check()
    (updates_dir / "coclody.exe-0.3.0.part").write_bytes(b"resto")
    removed = installer.cleanup_previous(exe, updates_dir)
    assert exe.with_name("coclody.exe.old") in removed
    assert not exe.with_name("coclody.exe.old").exists() and list(updates_dir.iterdir()) == []
    assert exe.read_bytes() == NEW_EXE
    assert installer.cleanup_previous(exe, updates_dir) == []
    assert installer.cleanup_previous(None, updates_dir / "no-existe") == []


# --- firma y manifiesto ----------------------------------------------------------------------


def test_signature_from_another_key_is_rejected(key, exe, updates_dir):
    release = Release(SigningKey.generate())  # firmado con una clave que la app no conoce
    up, http_ = make_updater(key, release.routes(), exe, updates_dir)
    result = up.check()
    assert result.status == "error" and "firma" in result.message
    assert release.url("coclody.exe") not in http_.requested  # ni se descarga el ejecutable
    assert_untouched(exe, updates_dir)


@pytest.mark.parametrize(
    "tamper",
    [
        lambda m: m.replace(b'"0.2.0"', b'"9.9.9"'),
        lambda m: m.replace(b'"windows-x64"', b'"macos-arm64"'),
        lambda m: m.replace(sha(NEW_EXE).encode(), sha(b"malware").encode()),
        lambda m: m + b" ",
        lambda m: m.replace(b"\n", b"\r\n"),
    ],
)
def test_manifest_altered_after_signing_is_rejected(key, exe, updates_dir, tamper):
    release = Release(key)
    altered = tamper(release.manifest)
    assert altered != release.manifest
    up, http_ = make_updater(key, release.routes(**{release.url(MANIFEST_NAME): altered}), exe, updates_dir)
    result = up.check()
    assert result.status == "error" and "firma" in result.message
    assert release.url("coclody.exe") not in http_.requested
    assert_untouched(exe, updates_dir)


@pytest.mark.parametrize(
    "signature",
    [b"", b"no-es-base64!!", base64.b64encode(b"corta"), base64.b64encode(b"x" * 64), b"A" * 2000],
)
def test_bad_signature_files(key, exe, updates_dir, signature):
    release = Release(key)
    up, _ = make_updater(key, release.routes(**{release.url(SIGNATURE_NAME): signature}), exe, updates_dir)
    assert up.check().status == "error"
    assert_untouched(exe, updates_dir)


@pytest.mark.parametrize("missing", [MANIFEST_NAME, SIGNATURE_NAME])
def test_release_without_signed_manifest(key, exe, updates_dir, missing):
    release = Release(key)
    routes = release.routes()
    data = json.loads(routes[API])
    data["assets"] = [a for a in data["assets"] if a["name"] != missing]
    routes[API] = json.dumps(data).encode()
    up, _ = make_updater(key, routes, exe, updates_dir)
    result = up.check()
    assert result.status == "error" and "manifiesto firmado" in result.message


def test_tag_must_match_manifest_version(key, exe, updates_dir):
    release = Release(key, version="0.2.0", tag="v0.3.0")
    up, _ = make_updater(key, release.routes(), exe, updates_dir)
    result = up.check()
    assert result.status == "error" and "no corresponde" in result.message
    assert_untouched(exe, updates_dir)


# --- versiones y plataforma -----------------------------------------------------------------


@pytest.mark.parametrize("version", ["0.1.0", "0.0.9", "0.0.1"])
def test_same_or_older_version_is_not_installed(key, exe, updates_dir, version):
    release = Release(key, version=version)
    up, http_ = make_updater(key, release.routes(), exe, updates_dir)
    assert up.check().status == "up_to_date"
    assert release.url("coclody.exe") not in http_.requested
    assert_untouched(exe, updates_dir)


def test_version_comparison_is_numeric(key, exe, updates_dir):
    release = Release(key, version="0.10.0")
    up, _ = make_updater(key, release.routes(), exe, updates_dir, current_version="0.9.9")
    assert up.check().status == "installed"


def test_other_platform_is_ignored(key, exe, updates_dir):
    release = Release(key, platform="macos-arm64")
    up, http_ = make_updater(key, release.routes(), exe, updates_dir)
    assert up.check().status == "up_to_date"
    assert release.url("coclody.exe") not in http_.requested


def test_from_source_only_reports(key, updates_dir):
    release = Release(key)
    up, http_ = make_updater(key, release.routes(), None, updates_dir)
    result = up.check()
    assert result.status == "available" and result.version == "0.2.0"
    assert release.url("coclody.exe") not in http_.requested


def test_check_without_install(key, exe, updates_dir):
    up, _ = make_updater(key, Release(key).routes(), exe, updates_dir)
    assert up.check(install=False).status == "available"
    assert_untouched(exe, updates_dir)


def test_without_public_key_nothing_happens(exe, updates_dir):
    http_ = FakeHttp({})
    up = Updater(REPO, updates_dir, http=http_, verify_key=None, executable=exe, platform_name="windows-x64")
    assert up.check().status == "disabled"
    assert http_.requested == []


def test_unknown_platform_disabled(key, exe, updates_dir):
    up, http_ = make_updater(key, Release(key).routes(), exe, updates_dir, platform_name=None)
    assert up.check().status == "disabled" and http_.requested == []


# --- red ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [NetworkError("Name or service not known"), NetworkError("timed out")],
)
def test_no_network_is_offline_and_harmless(key, exe, updates_dir, error):
    up, _ = make_updater(key, {API: error}, exe, updates_dir)
    result = up.check()
    assert result.status == "offline"
    assert_untouched(exe, updates_dir)


def test_real_urllib_without_network_is_offline(key, exe, updates_dir, monkeypatch):
    # El UrllibHttp real, con el opener fallando como cuando no hay red.
    def no_network(*args, **kwargs):
        raise urllib.error.URLError(OSError(8, "nodename nor servname provided"))

    http_ = UrllibHttp()
    monkeypatch.setattr(http_._opener, "open", no_network)
    up = Updater(REPO, updates_dir, http=http_, verify_key=key.verify_key, executable=exe, platform_name="windows-x64")
    assert up.check().status == "offline"
    assert_untouched(exe, updates_dir)


@pytest.mark.parametrize("cut_at", [0, 1, len(NEW_EXE) // 2, len(NEW_EXE) - 1])
def test_download_cut_halfway(key, exe, updates_dir, cut_at):
    release = Release(key)
    routes = release.routes(**{release.url("coclody.exe"): CutResponse(NEW_EXE, cut_at)})
    up, _ = make_updater(key, routes, exe, updates_dir)
    result = up.check()
    assert result.status == "offline" and "Sin conexión" in result.message
    assert_untouched(exe, updates_dir)  # sin .part, sin .new, sin .old

    retry, _ = make_updater(key, release.routes(), exe, updates_dir)
    assert retry.check().status == "installed"  # el próximo intento completa


def test_download_larger_than_manifest_is_cut(key, exe, updates_dir):
    release = Release(key)
    up, _ = make_updater(key, release.routes(**{release.url("coclody.exe"): NEW_EXE + b"extra"}), exe, updates_dir)
    result = up.check()
    assert result.status == "error" and "supera" in result.message
    assert_untouched(exe, updates_dir)


def test_download_with_wrong_content_same_size(key, exe, updates_dir):
    release = Release(key)
    evil = b"X" * len(NEW_EXE)
    up, _ = make_updater(key, release.routes(**{release.url("coclody.exe"): evil}), exe, updates_dir)
    result = up.check()
    assert result.status == "error" and "no coincide" in result.message
    assert_untouched(exe, updates_dir)


def test_published_size_mismatch(key, exe, updates_dir):
    release = Release(key)
    release.asset_size += 1
    up, http_ = make_updater(key, release.routes(), exe, updates_dir)
    assert up.check().status == "error"
    assert release.url("coclody.exe") not in http_.requested


@pytest.mark.parametrize("body", [b"<html>rate limited</html>", b"[]", b"\xff\xfe"])
def test_bad_github_response(key, exe, updates_dir, body):
    up, _ = make_updater(key, {API: body}, exe, updates_dir)
    assert up.check().status == "error"


def test_http_error_is_error_not_offline(key, exe, updates_dir):
    up, _ = make_updater(key, {}, exe, updates_dir)  # 404 en todo
    assert up.check().status == "error"


def test_only_https_scheme_is_registered():
    opener = UrllibHttp()._opener
    assert set(opener.handle_open) == {"https", "unknown"}
    with pytest.raises(DownloadError):
        UrllibHttp().get("http://example.com/x", 10)
    with pytest.raises(DownloadError):
        UrllibHttp().get("file:///etc/passwd", 10)


def test_redirect_to_http_is_refused():
    handler = _HttpsOnlyRedirects()
    req = urllib.request.Request("https://github.com/x")
    with pytest.raises(urllib.error.URLError):
        handler.redirect_request(req, None, 302, "Found", {}, "http://evil.example/x")
    assert handler.redirect_request(req, None, 302, "Found", {}, "https://objects.githubusercontent.com/x") is not None


def test_stream_to_file_limits(tmp_path):
    out = io.BytesIO()
    assert stream_to_file(io.BytesIO(b"abc"), out, 3) == sha(b"abc")
    with pytest.raises(DownloadError):
        stream_to_file(io.BytesIO(b"abcd"), io.BytesIO(), 3)
    with pytest.raises(NetworkError):
        stream_to_file(CutResponse(b"abcdef", 2), io.BytesIO(), 100)


# --- instalador ------------------------------------------------------------------------


def test_pending_previous_update_blocks_install(key, exe, updates_dir):
    old = exe.with_name("coclody.exe.old")
    old.mkdir()  # no se puede borrar con unlink: simula el .old en uso
    up, _ = make_updater(key, Release(key).routes(), exe, updates_dir)
    result = up.check()
    assert result.status == "error" and "reiniciá" in result.message
    assert exe.read_bytes() == OLD_EXE and not exe.with_name("coclody.exe.new").exists()


def test_failed_final_rename_rolls_back(key, exe, updates_dir, monkeypatch):
    real_replace = os.replace
    calls = []

    def flaky(src, dst):
        calls.append((Path(src).name, Path(dst).name))
        if Path(src).name == "coclody.exe.new":
            raise PermissionError("acceso denegado")
        return real_replace(src, dst)

    monkeypatch.setattr(installer.os, "replace", flaky)
    up, _ = make_updater(key, Release(key).routes(), exe, updates_dir)
    result = up.check()
    assert result.status == "error" and "no se pudo instalar" in result.message
    assert exe.read_bytes() == OLD_EXE  # restaurado desde .old
    assert ("coclody.exe.old", "coclody.exe") in calls
    assert not exe.with_name("coclody.exe.new").exists() and not exe.with_name("coclody.exe.old").exists()


def test_corrupted_copy_next_to_exe_is_rejected(key, exe, updates_dir, monkeypatch):
    def bad_copy(src, dst):
        Path(dst).write_bytes(b"corrupto")

    monkeypatch.setattr(installer.shutil, "copyfile", bad_copy)
    up, _ = make_updater(key, Release(key).routes(), exe, updates_dir)
    assert up.check().status == "error"
    assert_untouched(exe, updates_dir)


# --- manifiesto (unidad) ------------------------------------------------------------------


def good(**changes):
    data = {"schema": 1, "app": "coclody", "version": "1.2.3", "platform": "windows-x64",
            "asset": "coclody.exe", "size": 10, "sha256": "a" * 64}
    data.update(changes)
    return json.dumps(data).encode()


def test_parse_manifest_ok():
    m = parse_manifest(good())
    assert (m.version, m.platform, m.asset, m.size, m.version_tuple) == ("1.2.3", "windows-x64", "coclody.exe", 10, (1, 2, 3))


@pytest.mark.parametrize(
    "raw",
    [
        good(schema=2), good(schema=True), good(app="otra"), good(version="1.2"), good(version="v1.2.3"),
        good(version="01.2.3"), good(version="1.2.3-beta"), good(platform="linux-x64"), good(asset="../x.exe"),
        good(asset="dir/coclody.exe"), good(asset=".hidden"), good(size=0), good(size=-1), good(size="10"),
        good(size=True), good(size=10**12), good(sha256="A" * 64), good(sha256="a" * 63), good(extra=1),
        b"no json", b"[]", b"\xff", json.dumps({"schema": 1}).encode(), b" " * 20000,
    ],
)
def test_parse_manifest_rejects(raw):
    with pytest.raises(ManifestError):
        parse_manifest(raw)


def test_render_manifest_roundtrip_and_signature(key):
    raw = render_manifest("0.2.0", "windows-x64", "coclody.exe", 5, "b" * 64)
    assert verify_signed(raw, sign(key, raw), key.verify_key).version == "0.2.0"
    with pytest.raises(ManifestError):
        render_manifest("0.2", "windows-x64", "coclody.exe", 5, "b" * 64)


def test_parse_version_order():
    assert parse_version("0.10.0") > parse_version("0.9.9") > parse_version("0.9.0")


@pytest.mark.parametrize(
    ("plat", "machine", "expected"),
    [("win32", "AMD64", "windows-x64"), ("win32", "ARM64", None), ("darwin", "arm64", "macos-arm64"),
     ("darwin", "x86_64", None), ("linux", "x86_64", None)],
)
def test_current_platform(monkeypatch, plat, machine, expected):
    monkeypatch.setattr(sys, "platform", plat)
    monkeypatch.setattr("platform.machine", lambda: machine)
    assert current_platform() == expected


def test_release_verify_key(key):
    encoded = base64.b64encode(key.verify_key.encode()).decode()
    assert release_verify_key(encoded).encode() == key.verify_key.encode()
    assert release_verify_key("") is None
    assert release_verify_key("no-base64!") is None
    assert release_verify_key(base64.b64encode(b"corta").decode()) is None


def test_embedded_key_is_empty_or_valid():
    # Vale antes y después de pegar la clave en pubkey.py; lo que no puede pasar es una clave rota.
    from coclody.updater import pubkey

    assert (pubkey.release_verify_key() is None) == (pubkey.PUBLIC_KEY_B64.strip() == "")


def test_updater_defaults_are_safe(monkeypatch, updates_dir):
    up = Updater(REPO, updates_dir)
    assert up.executable is None  # desde fuente: nunca se reemplaza nada
    assert isinstance(up.http, UrllibHttp)
    assert client_module.MAX_RELEASE_JSON_BYTES == 1024 * 1024
