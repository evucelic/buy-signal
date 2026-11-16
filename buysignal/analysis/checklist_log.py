"""Append-only log of each session's checklist, for judging it on days it had not yet seen.

One JSON line per NYSE session. Rows are never rewritten, so later data revisions cannot leak in.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from buysignal.config import CHECKLIST_LOG, CHECKLIST_RULES
from buysignal.signals.buy_signal import CONTEXT_SIGNALS, REQUIRED_SIGNALS, BuySignal

_LOCK = threading.Lock()


def _number(value):
    if value is None:
        return None
    value = float(value)
    return value if pd.notna(value) else None


def _reading(signal, status=None) -> dict:
    item = {"value": _number(signal.score), "observed": signal.observed_at}
    if status:
        item["status"] = status
    if signal.name == "fed_rate":
        item["blocking"] = signal.metrics.get("blocking_horizons")
    if signal.name == "sector":
        item["growing"] = signal.metrics.get("growing")
        fetched = signal.history.fetched_at.iloc[-1] if signal.history is not None and "fetched_at" in signal.history else None
        item["retrieved"] = None if fetched is None or pd.isna(fetched) else str(fetched)
    if signal.name == "margin_debt":
        item["freshness"] = signal.metrics.get("freshness")
    return item


def row(result: BuySignal, session: str, now: datetime, etf_closes: dict | None = None) -> dict:
    """The log line for one session; `etf_closes` is {symbol: (close, date, currency)}."""
    by_name = {signal.name: signal for signal in result.subsignals}
    conditions = {}
    for name in REQUIRED_SIGNALS:
        signal = by_name.get(name)
        conditions[name] = _reading(signal, result.status(name)) if signal else {"status": result.status(name)}
    context = {name: _reading(by_name[name]) for name in CONTEXT_SIGNALS if name in by_name}
    market = by_name.get("market_dip")
    spy = {"close": _number(market.metrics.get("price")), "observed": market.observed_at} if market else None
    etfs = {symbol: {"close": _number(close), "observed": observed, "currency": currency}
            for symbol, (close, observed, currency) in (etf_closes or {}).items()}
    return {
        "session": session, "logged_at": now.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "rules": CHECKLIST_RULES, "count": result.passing_count, "of": result.required_count,
        "incomplete": result.state == "incomplete", "conditions": conditions, "context": context,
        "spy": spy, "etfs": etfs,
    }


def read(path: Path | None = None) -> list[dict]:
    """Every logged session, oldest first; a torn last line (a crash mid-write) is skipped."""
    try:
        text = Path(path or CHECKLIST_LOG).read_text()
    except FileNotFoundError:
        return []
    rows = []
    for line in text.splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def append(entry: dict, path: Path | None = None) -> bool:
    """Add one session's line unless that session is already logged. Returns whether it wrote."""
    path = Path(path or CHECKLIST_LOG)
    with _LOCK:
        if any(item.get("session") == entry["session"] for item in read(path)):
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        text = path.read_text() if path.exists() else ""
        line = json.dumps(entry, allow_nan=False, separators=(",", ":"))
        with path.open("a") as handle:
            # A torn line left by a crash would otherwise swallow this one.
            handle.write(("\n" if text and not text.endswith("\n") else "") + line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return True
