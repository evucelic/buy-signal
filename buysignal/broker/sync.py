"""Daily read of the IBKR statement into the portfolio store, driven by the refresh journal.

Without IBKR_FLEX_TOKEN and IBKR_FLEX_QUERY_ID set the collector does not exist, so an
unconfigured bot never reports a failing refresh.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import os

from buysignal.broker import flex

NAME = "ibkr"
RETRY_HOURS = 3  # a statement not yet covering the last weekday is asked for again after this


def configured() -> bool:
    return bool(os.environ.get("IBKR_FLEX_TOKEN") and os.environ.get("IBKR_FLEX_QUERY_ID"))


def _store(store):
    if store is not None:
        return store
    from buysignal.portfolio.store import PortfolioStore
    return PortfolioStore()


def update(store=None, *, now: datetime | None = None, fetch=flex.fetch) -> str | None:
    """Download, validate and store one statement; the error text on failure, else None."""
    now = now or datetime.now(timezone.utc)
    payload = fetch(os.environ["IBKR_FLEX_TOKEN"], os.environ["IBKR_FLEX_QUERY_ID"])
    statement = flex.parse(payload)
    _store(store).save_statement(statement, now)
    return None


def last_weekday(today: date) -> date:
    day = today - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def demands_refresh(store=None, *, now: datetime | None = None) -> bool:
    """True while the stored statement ends before the last weekday and no read happened lately."""
    now = now or datetime.now(timezone.utc)
    account = _store(store).account()
    if not account.get("statement_to"):
        return True
    if date.fromisoformat(account["statement_to"]) >= last_weekday(now.date()):
        return False
    synced = datetime.fromisoformat(account["synced_at"]) if account.get("synced_at") else None
    return synced is None or now - synced >= timedelta(hours=RETRY_HOURS)


TOKEN_WARNING_DAYS = 14


def token_days_left(today: date) -> int | None:
    """Days until the Flex token expires, from IBKR_FLEX_TOKEN_EXPIRES (YYYY-MM-DD); None when not set.

    IBKR never says when a token expires, so the date chosen when generating it is kept here.
    """
    value = os.environ.get("IBKR_FLEX_TOKEN_EXPIRES", "").strip()
    try:
        return (date.fromisoformat(value) - today).days if value else None
    except ValueError:
        return None
