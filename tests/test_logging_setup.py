from __future__ import annotations

import logging
import os
import stat

import pytest

from coclody.logging_setup import configure_app_log

SECRET = "123456789:AAH" + "s" * 32


@pytest.fixture
def app_log(app_paths):
    configure_app_log(app_paths.app_log, secrets=[SECRET], console=False)
    yield app_paths.app_log
    root = logging.getLogger()
    for h in [h for h in root.handlers if getattr(h, "_coclody", False)]:
        root.removeHandler(h)
        h.close()


def read(path):
    for h in logging.getLogger().handlers:
        h.flush()
    return path.read_text(encoding="utf-8")


def test_secret_is_redacted_in_messages_and_args(app_log):
    log = logging.getLogger("coclody.test")
    log.warning("POST https://api.telegram.org/bot%s/getMe", SECRET)
    log.warning(f"directo {SECRET}")
    text = read(app_log)
    assert SECRET not in text and text.count("<redacted>") == 2


def test_secret_is_redacted_in_tracebacks(app_log):
    try:
        raise RuntimeError(f"falló con {SECRET}")
    except RuntimeError:
        logging.getLogger("coclody.test").exception("algo")
    text = read(app_log)
    assert SECRET not in text and "RuntimeError" in text


def test_httpx_info_is_silenced(app_log):
    logging.getLogger("httpx").info("HTTP Request: POST https://api.telegram.org/botXYZ/getUpdates")
    assert "getUpdates" not in read(app_log)


def test_reconfigure_does_not_duplicate(app_log, app_paths):
    configure_app_log(app_paths.app_log, secrets=[SECRET], console=False)
    logging.getLogger("coclody.test").warning("una vez")
    assert read(app_log).count("una vez") == 1


@pytest.mark.skipif(os.name != "posix", reason="permisos POSIX")
def test_app_log_is_private(app_log):
    assert stat.S_IMODE(app_log.stat().st_mode) == 0o600


def test_secret_part_of_token_is_redacted_even_url_encoded(app_log):
    from urllib.parse import quote

    logging.getLogger("coclody.test").warning("url: https://api.telegram.org/bot%s/getMe", quote(SECRET, safe=""))
    text = read(app_log)
    assert SECRET.split(":", 1)[1] not in text and "<redacted>" in text


def test_redact_function_uses_registered_secrets(app_log):
    from coclody.logging_setup import redact

    assert redact(f"The token `{SECRET}` was rejected") == "The token `<redacted>` was rejected"
    assert SECRET.split(":", 1)[1] not in redact("a%3A" + SECRET.split(":", 1)[1])
    assert redact("nada que tapar") == "nada que tapar"


def test_short_tails_are_not_treated_as_secrets(app_paths):
    from coclody.logging_setup import _variants

    assert _variants(["123:abc"]) == ("123:abc",)  # "abc" solo taparía texto legítimo
