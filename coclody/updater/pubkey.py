"""Clave pública Ed25519 con la que se verifican los releases.

Es pública: puede (y tiene que) estar en el repo. La privada vive solo en el
llavero de quien firma (``tools/sign_release.py keygen``) y nunca en GitHub.

Mientras esté vacía, el updater no instala nada: sin clave no hay forma de
verificar un release.
"""

from __future__ import annotations

import base64

from nacl.signing import VerifyKey

# Pegar acá la salida de `python tools/sign_release.py keygen` (base64, 32 bytes).
PUBLIC_KEY_B64 = "hZIR7OmJjbYx0IRR9U06RAGyDnWkJ86dVGD4k9JGswE="


def release_verify_key(encoded: str | None = None) -> VerifyKey | None:
    """La clave de verificación, o None si no está configurada o es inválida."""
    value = (PUBLIC_KEY_B64 if encoded is None else encoded).strip()
    if not value:
        return None
    try:
        raw = base64.b64decode(value, validate=True)
    except ValueError:
        return None
    return VerifyKey(raw) if len(raw) == 32 else None
