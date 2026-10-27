from __future__ import annotations

import logging
import os
import stat

import pytest

from coclody.security import SECURITY_LOGGER, ChatGuard, close_security_log, configure_security_log

ME = 111


@pytest.fixture
def sec_log(app_paths):
    logger = configure_security_log(app_paths.security_log)
    yield app_paths.security_log
    close_security_log()


@pytest.fixture
def guard(sec_log):
    return ChatGuard([ME, 222])


def lines(path):
    for handler in logging.getLogger(SECURITY_LOGGER).handlers:
        handler.flush()
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def test_authorized_private_chat_is_not_logged(guard, sec_log):
    assert guard.is_authorized(ME, ME, "private") is True
    assert lines(sec_log) == []


@pytest.mark.parametrize(
    ("chat_id", "user_id", "chat_type", "reason"),
    [
        (999, 999, "private", "not_in_whitelist"),
        (ME, 999, "private", "user_chat_mismatch"),
        (ME, ME, "group", "not_private_chat"),
        (-100123, ME, "supergroup", "not_private_chat"),
        (ME, ME, "channel", "not_private_chat"),
        (None, ME, "private", "missing_or_invalid_id"),
        (ME, None, "private", "missing_or_invalid_id"),
        ("111", ME, "private", "missing_or_invalid_id"),
        (True, True, "private", "missing_or_invalid_id"),
    ],
)
def test_rejections_are_logged(guard, sec_log, chat_id, user_id, chat_type, reason):
    assert guard.is_authorized(chat_id, user_id, chat_type) is False
    (line,) = lines(sec_log)
    assert "WARNING rechazado" in line
    assert f"reason={reason}" in line


def test_log_line_fields(guard, sec_log):
    guard.is_authorized(999, 999, "private", update_kind="callback_query")
    (line,) = lines(sec_log)
    assert "chat_id=999 user_id=999 chat_type=private update=callback_query" in line


def test_log_cannot_be_injected(guard, sec_log):
    guard.is_authorized("1\nFAKE OK", 999, "private\nINFO ok", update_kind="x\r\nINFO admitido")
    (line,) = lines(sec_log)
    assert "chat_id=<str>" in line
    assert "chat_type=private?info?ok" in line
    assert "update=x??info?admitido" in line


def test_whitelist_change_requires_new_guard(sec_log):
    ids = [ME]
    guard = ChatGuard(ids)
    ids.append(999)
    assert guard.is_authorized(999, 999, "private") is False


@pytest.mark.parametrize("ids", [[0], [-5], [True], ["111"]])
def test_guard_rejects_bad_whitelist(ids):
    with pytest.raises(ValueError):
        ChatGuard(ids)


def test_empty_whitelist_rejects_everyone(sec_log):
    assert ChatGuard([]).is_authorized(ME, ME, "private") is False


def test_reconfigure_does_not_duplicate(app_paths, guard):
    configure_security_log(app_paths.security_log)
    guard.is_authorized(999, 999, "private")
    assert len(lines(app_paths.security_log)) == 1
    assert len(logging.getLogger(SECURITY_LOGGER).handlers) == 1


def test_security_log_is_separate_and_private(guard, sec_log, caplog):
    with caplog.at_level(logging.WARNING):
        guard.is_authorized(999, 999, "private")
    assert caplog.records == []  # no se propaga al log general
    if os.name == "posix":
        assert stat.S_IMODE(sec_log.stat().st_mode) == 0o600


# --- límite de registro de rechazos -----------------------------------------------------------


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_rejection_logging_is_rate_limited_per_chat(sec_log):
    from coclody.security import LOG_BURST, LOG_WINDOW_S

    clock = Clock()
    guard = ChatGuard([ME], clock=clock)
    for _ in range(50):
        assert guard.is_authorized(999, 999, "private") is False  # se rechaza siempre
    assert len(lines(sec_log)) == LOG_BURST
    assert guard.is_authorized(888, 888, "private") is False  # otro chat tiene su propio cupo
    assert len(lines(sec_log)) == LOG_BURST + 1

    clock.now += LOG_WINDOW_S
    guard.is_authorized(999, 999, "private")
    summary, rejected = lines(sec_log)[-2:]
    assert f"suprimidos chat_id=999 rechazos={50 - LOG_BURST}" in summary
    assert "rechazado reason=not_in_whitelist chat_id=999" in rejected


def test_authorized_chats_are_never_limited(sec_log):
    guard = ChatGuard([ME], clock=Clock())
    assert all(guard.is_authorized(ME, ME, "private") for _ in range(100))
    assert lines(sec_log) == []


def test_many_distinct_chats_do_not_grow_memory_unbounded(sec_log):
    from coclody import security

    guard = ChatGuard([ME], clock=Clock())
    for chat in range(1, security._MAX_TRACKED_CHATS * 3):
        guard.is_authorized(10_000 + chat, 10_000 + chat, "private")
    assert len(guard._windows) <= security._MAX_TRACKED_CHATS


def test_permits_does_not_log(sec_log):
    guard = ChatGuard([ME])
    assert guard.permits(ME, ME, "private") is True
    assert guard.permits(999, 999, "private") is False
    assert lines(sec_log) == []
