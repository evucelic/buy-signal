"""Tests for logs.py: the handlers it installs, where they write, and the level they honour."""

import logging
import sys
import threading

import pytest

from buysignal import logs


@pytest.fixture(autouse=True)
def restore_root_logger():
    """configure() mutates loggers that outlive any one test."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    noisy = {name: logging.getLogger(name).level for name in logs._NOISY}
    hooks = sys.excepthook, threading.excepthook
    yield
    root.handlers = handlers
    root.setLevel(level)
    for name, restored in noisy.items():
        logging.getLogger(name).setLevel(restored)
    sys.excepthook, threading.excepthook = hooks
    logging.captureWarnings(False)


def _ours(root=None):
    root = root or logging.getLogger()
    return [h for h in root.handlers if getattr(h, logs._OURS, False)]


def test_configure_installs_a_stream_and_a_file_handler(tmp_path):
    logs.configure(tmp_path / "buysignal.log")

    assert len(_ours()) == 2


def test_configure_is_idempotent(tmp_path):
    """__main__.py calls it once, but a re-import or a second entrypoint must not double every line."""
    logs.configure(tmp_path / "buysignal.log")
    logs.configure(tmp_path / "buysignal.log")

    assert len(_ours()) == 2


def test_configure_writes_records_to_the_configured_path(tmp_path):
    path = tmp_path / "buysignal.log"
    logs.configure(path)

    logging.getLogger("buysignal.collectors.fed_rate").info("FedWatch cache updated")

    written = path.read_text()
    assert "FedWatch cache updated" in written
    assert "buysignal.collectors.fed_rate" in written
    assert "INFO" in written


def test_configure_honours_the_log_level_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    logs.configure(tmp_path / "buysignal.log")

    assert logging.getLogger().level == logging.DEBUG


def test_configure_defaults_to_info(tmp_path, monkeypatch):
    monkeypatch.delenv("LOG_LEVEL", raising=False)

    logs.configure(tmp_path / "buysignal.log")

    assert logging.getLogger().level == logging.INFO


def test_configure_quiets_the_noisy_libraries_even_at_debug(tmp_path, monkeypatch):
    """LOG_LEVEL=DEBUG is for our own retry chatter, not a line per socket from urllib3."""
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    logs.configure(tmp_path / "buysignal.log")

    assert logging.getLogger("urllib3").level == logging.WARNING


def test_configure_keeps_logging_to_stderr_when_the_file_cannot_be_opened(tmp_path):
    unwritable = tmp_path / "file.txt"
    unwritable.write_text("not a directory")

    logs.configure(unwritable / "buysignal.log")

    assert len(_ours()) == 1


def test_log_lines_tracebacks_and_uncaught_thread_errors_are_redacted(tmp_path, monkeypatch):
    token = "123456789:AAFakeTelegramTokenForTestsOnly_abcdefg"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    path = tmp_path / "buysignal.log"
    logs.configure(path)
    log = logging.getLogger("buysignal.test")
    log.warning("GET https://api.telegram.org/bot%s/getUpdates failed", token)
    try:
        raise ValueError(f"bad url https://api.telegram.org/bot{token}/sendMessage")
    except ValueError:
        log.exception("send failed")
    worker = threading.Thread(target=lambda: (_ for _ in ()).throw(RuntimeError(f"boom {token}")), name="poller")
    worker.start()
    worker.join()
    for handler in _ours():
        handler.flush()
    text = path.read_text()
    assert "send failed" in text and "Uncaught exception in thread poller" in text
    assert token not in text and text.count("[redacted]") >= 3
