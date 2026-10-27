"""HTTP mínimo para el updater, con la biblioteca estándar.

- Solo HTTPS, también en las redirecciones (GitHub redirige las descargas).
- Certificados de certifi: el mismo almacén en macOS, Windows y dentro del
  ejecutable de PyInstaller.
- Límite de bytes en todo: una respuesta más grande de lo esperado se corta.
- Cualquier problema de red se reporta como ``NetworkError``: el llamador lo
  trata como "sin internet" y la app sigue funcionando.
"""

from __future__ import annotations

import hashlib
import http.client
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from typing import IO, Protocol

import certifi

from coclody import __version__

TIMEOUT_S = 20
_CHUNK = 64 * 1024


class NetworkError(Exception):
    pass


class DownloadError(Exception):
    pass


class Http(Protocol):
    def get(self, url: str, max_bytes: int, accept: str = "*/*") -> bytes: ...

    def download(self, url: str, dest: Path, max_bytes: int) -> str:
        """Descarga a ``dest`` y devuelve el sha256 (hex). Borra ``dest`` si falla."""
        ...


class _HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.lower().startswith("https://"):
            raise urllib.error.URLError(f"redirección a una URL que no es HTTPS: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class UrllibHttp:
    def __init__(self, timeout_s: float = TIMEOUT_S):
        context = ssl.create_default_context(cafile=certifi.where())
        # Opener armado a mano (build_opener agrega http://, file://, ftp://, data://):
        # acá solo existe HTTPS. Cualquier otro esquema da "unknown url type".
        self._opener = urllib.request.OpenerDirector()
        for handler in (
            urllib.request.HTTPSHandler(context=context),
            _HttpsOnlyRedirects(),
            urllib.request.HTTPErrorProcessor(),
            urllib.request.HTTPDefaultErrorHandler(),
            urllib.request.UnknownHandler(),
        ):
            self._opener.add_handler(handler)
        self._timeout = timeout_s

    def get(self, url: str, max_bytes: int, accept: str = "*/*") -> bytes:
        with self._open(url, accept) as response:
            try:
                data = response.read(max_bytes + 1)
            except (OSError, http.client.HTTPException) as exc:
                raise NetworkError(f"respuesta interrumpida: {exc}") from None
        if len(data) > max_bytes:
            raise DownloadError(f"la respuesta supera {max_bytes} bytes")
        return data

    def download(self, url: str, dest: Path, max_bytes: int) -> str:
        try:
            with self._open(url, "application/octet-stream") as response:
                with dest.open("wb") as fh:
                    return stream_to_file(response, fh, max_bytes)
        except BaseException:
            dest.unlink(missing_ok=True)
            raise

    def _open(self, url: str, accept: str):
        if not url.lower().startswith("https://"):
            raise DownloadError(f"solo se admiten URLs HTTPS: {url}")
        request = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": f"CoClody/{__version__}"})
        try:
            return self._opener.open(request, timeout=self._timeout)
        except urllib.error.HTTPError as exc:
            raise DownloadError(f"HTTP {exc.code} en {url}") from None
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            raise NetworkError(str(exc)) from None


def stream_to_file(response: IO[bytes], fh: IO[bytes], max_bytes: int) -> str:
    """Copia la respuesta al archivo calculando el sha256. Corta si excede ``max_bytes``."""
    digest = hashlib.sha256()
    total = 0
    try:
        while chunk := response.read(_CHUNK):
            total += len(chunk)
            if total > max_bytes:
                raise DownloadError(f"la descarga supera {max_bytes} bytes")
            digest.update(chunk)
            fh.write(chunk)
    except (OSError, http.client.HTTPException) as exc:  # conexión cortada a mitad de camino
        raise NetworkError(f"descarga interrumpida: {exc}") from None
    return digest.hexdigest()
