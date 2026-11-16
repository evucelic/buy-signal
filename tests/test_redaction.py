"""Credentials never reach the refresh journal, a Telegram message or the browser."""
from datetime import datetime, timezone

from buysignal.redaction import public_env, redact, redact_strings
from buysignal.schedule.journal import RefreshJournal

TOKEN = "123456789:AAFakeTelegramTokenForTestsOnly_abcdefg"
FLEX = "987654321098765432109876"
NOW = datetime(2026, 11, 17, 12, tzinfo=timezone.utc)


def _secrets(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("IBKR_FLEX_TOKEN", FLEX)


def test_tokens_are_redacted_as_written_and_as_encoded_in_a_url(monkeypatch):
    _secrets(monkeypatch)
    assert FLEX not in redact(f"HTTPError: 500 for url: https://host/FlexWebService/SendRequest?t={FLEX}&q=1&v=3")
    assert redact(f"https://api.telegram.org/bot{TOKEN}/sendMessage") == "https://api.telegram.org/bot[redacted]/sendMessage"
    assert redact(TOKEN.replace(":", "%3A")) == "[redacted]"
    monkeypatch.setenv("IBKR_FLEX_TOKEN", "")
    assert redact("nothing set") == "nothing set"  # an empty variable blanks nothing


def test_every_string_in_an_outgoing_payload_is_redacted(monkeypatch):
    _secrets(monkeypatch)
    payload = {"text": f"⚠️ failed: ?t={FLEX}", "media": {"caption": TOKEN},
               "reply_markup": {"inline_keyboard": [[{"text": FLEX}]]}, "chat_id": 42}
    clean = redact_strings(payload)
    assert FLEX not in str(clean) and TOKEN not in str(clean) and clean["chat_id"] == 42
    assert payload["text"].endswith(FLEX)  # the caller's payload is left as it was


def test_journal_errors_are_stored_redacted(monkeypatch, tmp_path):
    _secrets(monkeypatch)
    path = tmp_path / "journal.json"
    RefreshJournal(path).record("ibkr", ok=False, error=f"HTTPError for ?t={FLEX}", now=NOW)
    assert FLEX not in path.read_text()
    assert RefreshJournal(path).statuses()[0].error == "HTTPError for ?t=[redacted]"


def test_the_browser_gets_no_credentials(monkeypatch):
    _secrets(monkeypatch)
    env = public_env()
    assert "TELEGRAM_BOT_TOKEN" not in env and "IBKR_FLEX_TOKEN" not in env and "PATH" in env
