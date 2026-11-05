"""Regular-session deadlines for the user's venues, including holidays and early closes."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
import os

import pandas_market_calendars as mcal

from buysignal.portfolio.instruments import INSTRUMENTS


@dataclass(frozen=True)
class Session:
    exchange: str
    session_date: date
    closes: datetime
    alert_at: datetime


@lru_cache(maxsize=64)
def calendar_sessions(exchange, day, minutes=60):
    if not 1 <= minutes <= 240:
        raise ValueError('PRECLOSE_MINUTES must be between 1 and 240.')
    calendar = mcal.get_calendar(exchange)
    schedule = calendar.schedule(start_date=day, end_date=day + timedelta(days=14))
    result = []
    for stamp, row in schedule.iterrows():
        opens = row.market_open.to_pydatetime()
        closes = row.market_close.to_pydatetime()
        result.append(Session(exchange, stamp.date(), closes, max(opens, closes - timedelta(minutes=minutes))))
    return tuple(result)


def upcoming(now):
    now = now.astimezone(timezone.utc)
    minutes = int(os.environ.get('PRECLOSE_MINUTES', '60'))
    result = []
    for exchange in dict.fromkeys(i.exchange for i in INSTRUMENTS):
        result.extend(calendar_sessions(exchange, now.date(), minutes))
    return result


def due(now, store):
    now = now.astimezone(timezone.utc)
    return [s for s in upcoming(now) if s.alert_at <= now < s.closes and not store.delivered(s.exchange, str(s.session_date))]


def next_delay(now, store):
    sessions = upcoming(now)
    if any(s.alert_at <= now < s.closes and not store.delivered(s.exchange, str(s.session_date)) for s in sessions):
        return 60  # failed delivery retries while there is still time to act
    future = [(s.alert_at - now).total_seconds() for s in sessions if s.alert_at > now]
    # Check at most hourly while quiet; wake exactly at a nearer deadline.
    return max(.1, min(3600, min(future))) if future else 3600
