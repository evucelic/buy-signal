"""Overview and panel captions: every number behind a reading, laid out to scan; no collection or IO.

Tables are monospace and stay within about 32 characters so a phone does not wrap them; warnings
for missing or stale data stay in the message.
"""
from __future__ import annotations

from datetime import datetime
import os
from zoneinfo import ZoneInfo

import pandas as pd

from buysignal.analysis.signal_charts import DEFAULT_RANGE, RANGES
from buysignal.config import SECTOR_MIN_ESTIMATE_COVERAGE, VIX_SOFT, VIX_STRONG
from buysignal.signals.base import SubSignal
from buysignal.signals.buy_signal import BuySignal, INCOMPLETE, REQUIRED_SIGNALS, usable
from buysignal.telegram_bot.render import esc, fmt

# Buttons in checklist order, then the context readings.
SECTIONS = (("vix", "VIX"), ("fed_rate", "Fed"), ("margin_debt", "Margin"), ("market_dip", "Index"),
            ("sector", "EPS"), ("credit", "Credit"), ("yield_curve", "Curve"))
INDEXES = ("SPY", "NASDAQ", "DOW", "EURO50")
INDEX_NAMES = {"SPY": "S&P 500 (SPY)", "NASDAQ": "Nasdaq", "DOW": "Dow Jones", "EURO50": "Euro Stoxx 50"}
_INDEX_BUTTONS = {"NASDAQ": "Nasdaq", "DOW": "Dow", "EURO50": "Euro50"}
TITLES = {"vix": "VIX", "fed_rate": "Fed outlook", "margin_debt": "Margin debt",
          "sector": "Earnings outlook", "credit": "Credit spread", "yield_curve": "Yield curve"}
VIEWS = {name for name, _ in SECTIONS} | {"close"} | {"index_" + name for name in INDEXES}
# Retired view names that old reports' buttons still send.
LEGACY_VIEWS = {"market_info", "eps_info", "fed_changes"}
_SELECTED = "● "
_HISTORY_ROWS = 6  # recent observations listed in a monthly panel

# Checklist rows: the condition's short name, and its box for pass, fail and no current data.
_CHECK_NAMES = {"vix": "Fear", "fed_rate": "Fed", "margin_debt": "Margin", "market_dip": "Dip", "sector": "EPS"}
_BOXES = {"pass": "[x]", "fail": "[ ]", "incomplete": "[?]"}
_CURVE_STATES = {"steep": "steep", "flat": "flat", "inverted": "inverted", "deep_inversion": "deeply inverted"}


def stamp(value, *, short=False):
    if value is None or pd.isna(value):
        return "—"
    value = pd.Timestamp(value)
    if value.tzinfo is None:
        return value.strftime("%Y-%m-%d")
    value = value.tz_convert(ZoneInfo(os.environ.get("TELEGRAM_TIMEZONE", "Europe/Zagreb")))
    return value.strftime("%m-%d %H:%M" if short else "%Y-%m-%d %H:%M %Z")


def percent(value):
    return fmt(value, "+.1%")


def split_view(view: str | None) -> tuple[str | None, str]:
    """'index_DOW@5y' -> ('index_DOW', '5y'); the default range carries no suffix."""
    if not view:
        return None, DEFAULT_RANGE
    base, _, chosen = view.partition("@")
    return base, chosen if chosen in RANGES else DEFAULT_RANGE


def view_name(base: str, range_key: str = DEFAULT_RANGE) -> str:
    return base if range_key == DEFAULT_RANGE else f"{base}@{range_key}"


def is_view(requested: str) -> bool:
    base, _, chosen = requested.partition("@")
    return (base in VIEWS and (not chosen or chosen in RANGES)) or requested in LEGACY_VIEWS


def section_of(base: str) -> str:
    return "market_dip" if base.startswith("index_") else base


def _year(value) -> str:
    return str(pd.Timestamp(value).year) if value else "the start"


def _share(value: float) -> str:
    # Never round a rare reading to "0%" or a common one to "100%".
    if value < .01:
        return "under 1%"
    if value > .99:
        return "over 99%"
    return f"{value:.0%}"


def _price(value: float) -> str:
    return f"{value:,.0f}" if value >= 1000 else f"{value:,.2f}"


def _lines(rows, notes: bool = False) -> list[str]:
    """Align rows of cells: labels left, values right. () is a blank line, a one-cell row a heading.

    With notes=True the last column is free text and stays left-aligned.
    """
    cells = [row for row in rows if len(row) > 1]
    count = max((len(row) for row in cells), default=0)
    widths = [max((len(row[i]) for row in cells if len(row) > i), default=0) for i in range(count)]
    lines = []
    for row in rows:
        if len(row) <= 1:
            lines.append(row[0] if row else "")
            continue
        parts = [row[0].ljust(widths[0])]
        for i, cell in enumerate(row[1:], 1):
            parts.append(cell.ljust(widths[i]) if notes and i == count - 1 else cell.rjust(widths[i]))
        lines.append("  ".join(parts).rstrip())
    return lines


def _pre(lines: list[str]) -> str:
    return "<pre>" + esc("\n".join(lines)) + "</pre>"


def _since(year, *rows) -> list[tuple]:
    """Rows that describe a reading against its own history, under a 'Since <year>' heading."""
    return [(), (f"Since {year}",), *rows] if rows else []


def _fed_rows(signal: SubSignal):
    """(meeting date, likeliest outcome, its odds) per meeting; a tie with "higher" reads as higher."""
    frame = signal.history
    if frame is None or frame.empty:
        return []
    frame = frame.assign(meeting_date=pd.to_datetime(frame["meeting_date"])).sort_values("meeting_date")
    rows = []
    for row in frame.itertuples():
        options = [("lower", row.prob_ease), ("same", row.prob_no_change), ("higher", row.prob_hike)]
        leading = max(value for _, value in options)
        label = "higher" if row.prob_hike >= leading else max(options, key=lambda item: item[1])[0]
        rows.append((row.meeting_date, label, leading))
    return rows


def _index_facts(signal: SubSignal, name: str = "SPY") -> dict:
    facts = signal.metrics.get("index_facts", {}).get(name)
    if facts:
        return facts
    # Signals built before per-index facts existed describe SPY at the top level.
    return signal.metrics if name == "SPY" else {}


def verdict(signal: SubSignal) -> str:
    """What the reading says, without the measure's name or its numbers."""
    m = signal.metrics
    if signal.name == "market_dip":
        return "in a dip" if signal.passes else "no dip"
    if signal.name == "vix":
        return "high fear" if signal.score >= VIX_STRONG else "fear elevated" if signal.score >= VIX_SOFT else "calm"
    if signal.name == "fed_rate":
        rows = _fed_rows(signal)
        higher = [f"{date:%b %Y}" for date, label, _ in rows if label == "higher"]
        if higher:
            return f"higher rates expected by {higher[0]}"
        return "no rate rise expected" if rows else "odds unavailable"
    if signal.name == "margin_debt":
        direction = "falling" if signal.score < 0 else "rising" if signal.score > 0 else "unchanged"
        return direction + (" · ⚠ stale" if m.get("freshness") == "stale" else "")
    if signal.name == "sector":
        return f"{m.get('growing', '—')} of {m.get('sample_size', '—')} industries growing"
    if signal.name == "yield_curve":
        return _CURVE_STATES.get(signal.state, signal.state)
    # Credit (calm / stressed) already speaks plainly.
    return signal.state.replace("_", " ")


def _check_value(signal: SubSignal) -> str:
    """The number behind one checklist condition, short enough for a 32-character row."""
    m = signal.metrics
    if signal.name == "vix":
        return f"VIX {signal.score:.1f}" + (" high" if signal.score >= VIX_STRONG else "")
    if signal.name == "fed_rate":
        rows = _fed_rows(signal)
        if not rows:
            return "odds unavailable"
        date, label, odds = next((row for row in rows if row[1] == "higher"), rows[0])
        return f"{label} {odds:.0%} {date:%b %y}"
    if signal.name == "margin_debt":
        month = pd.Timestamp(signal.observed_at).strftime("%b") if signal.observed_at else "latest"
        return f"{signal.score:+.1%} in {month}"
    if signal.name == "market_dip":
        return f"SPY {signal.score:+.1%} vs high"
    if signal.name == "sector":
        return f"{m.get('growing', '—')} of {m.get('sample_size', '—')} up"
    return f"{signal.score:+.2f}"


def _sheet(result: BuySignal, by_name: dict) -> list[str]:
    """The data sheet: the checklist, then context, then the index moves."""
    checks = []
    for name in REQUIRED_SIGNALS:
        signal, status = by_name.get(name), result.status(name)
        # [?] marks a reading that is missing or stale; the warning under the sheet names it.
        value = _check_value(signal) if signal is not None else "no data"
        checks.append((f"{_BOXES[status]} {_CHECK_NAMES[name]}", value))
    credit, curve = by_name.get("credit"), by_name.get("yield_curve")
    context = [("Credit", f"{credit.score * 100:.0f} bp" if credit else "no data"),
               ("Curve", f"{curve.score:+.2f} pp" if curve else "no data")]
    lines = [f"CHECKLIST  {result.label}", *_lines(checks, notes=True), "",
             "CONTEXT", *_lines(context, notes=True)]
    market = by_name.get("market_dip")
    changes = market.metrics.get("index_changes", {}) if market else {}
    if changes:
        rows = [("", "Day", "Week", "Month")]
        rows += [(_INDEX_BUTTONS.get(name, name), *[f"{value:+.1%}" for value in item["changes"]]) for name, item in changes.items()]
        lines += ["", *_lines(rows)]
    return lines


def _sources(by_name: dict, caches, now: datetime) -> str:
    refreshed = dict(caches)
    labels = (("market_dip", "Prices"), ("vix", "VIX"), ("fed_rate", "Fed"), ("margin_debt", "Margin"),
              ("sector", "Earnings"), ("credit", "Credit"), ("yield_curve", "Curve"))
    lines = ["<b>Data as of</b> · (last refresh)"]
    for key, label in labels:
        signal = by_name.get(key)
        observed = stamp(signal.observed_at if signal else None, short=True)
        if signal and key == "sector":
            # Forecasts have no observation date; the day they were retrieved stands in for it.
            fetched = signal.history.fetched_at.iloc[-1] if signal.history is not None and "fetched_at" in signal.history else None
            observed = stamp(fetched, short=True)
        elif signal and key == "margin_debt" and signal.observed_at:
            observed = pd.Timestamp(signal.observed_at).strftime("%b %Y")
        fetched = signal.metrics.get("retrieved_at") if signal and key == "market_dip" else None
        lines.append(f"{label} {esc(observed)} ({esc(stamp(fetched or refreshed.get(key), short=True))})")
    lines.append("Report " + esc(stamp(now)))
    return "<blockquote expandable>" + "\n".join(lines) + "</blockquote>"


def overview(result: BuySignal, caches, now: datetime):
    by_name = {s.name: s for s in result.subsignals}
    lines = [_pre(_sheet(result, by_name))]
    if result.state == INCOMPLETE:
        unknown = [_CHECK_NAMES[name] for name in REQUIRED_SIGNALS if not usable(by_name.get(name))]
        lines.append("⚠️ No current data: " + esc(", ".join(unknown)))
    market = by_name.get("market_dip")
    if market:
        m = market.metrics
        if m.get("stale"):
            lines.append(f"⚠️ Prices cached through {esc(market.observed_at or 'unknown')}; expected {esc(m.get('expected_session') or 'latest session')}.")
        elif m.get("refresh_failed"):
            lines.append("⚠️ Price refresh failed; showing cached observations.")
        changes = m.get("index_changes", {})
        if len({item["date"] for item in changes.values()}) > 1:
            lines.append("Index dates: " + esc(" · ".join(f"{name} {item['date']}" for name, item in changes.items())))
    lines.extend(["", _sources(by_name, caches, now)])
    return "\n".join(lines)


def keyboard(token, view=None, available_indexes=INDEXES, sections=None, ranges=()):
    def button(label, key, selected=False):
        return {"text": (_SELECTED if selected else "") + label, "callback_data": f"r:{token}:{key}"}
    base, chosen = split_view(view)
    current = section_of(base) if base else None
    rows = []
    if view and len(ranges) > 1:
        rows.append([button(RANGES[key]["label"], view_name(base, key), key == chosen) for key in ranges])
    if current == "market_dip":
        shown = base.removeprefix("index_") if base.startswith("index_") else "SPY"
        rows.append([button(_INDEX_BUTTONS.get(name, name), view_name("market_dip" if name == "SPY" else "index_" + name, chosen), name == shown)
                     for name in available_indexes])
    choices = [(key, label) for key, label in SECTIONS if sections is None or key in sections]
    if view and len(choices) == 1:
        choices = []
    buttons = [button(label, key, key == current) for key, label in choices]
    if view:
        buttons.append(button("✕ Close", "close"))
    rows.extend(buttons[i:i + 4] for i in range(0, len(buttons), 4))
    return {"inline_keyboard": rows}


# --- panels: the numbers behind each reading ---------------------------------------------------


def _day(value) -> str:
    """'9 Dec 26' with the day padded, so a column of dates lines up."""
    value = pd.Timestamp(value)
    return f"{value.day:>2} {value:%b %y}"


def _earlier(series, sessions: int) -> float | None:
    return float(series.iloc[-1 - sessions]) if series is not None and len(series) > sessions else None


def _market_rows(signal: SubSignal, name: str) -> list[tuple]:
    facts = _index_facts(signal, name)
    if not facts:
        return []
    rows = [("Price", _price(facts["price"]))]
    if facts.get("peak") is not None:
        rows.append(("3-month high", _price(facts["peak"])))
        if facts.get("peak_date"):
            rows.append(("High set", f"{pd.Timestamp(facts['peak_date']):%-d %b}"))
    rows.append(("From high", f"{facts.get('drawdown', 0.):+.1%}"))
    rows += [("Day", percent(facts.get("daily"))), ("Week", percent(facts.get("weekly"))), ("Month", percent(facts.get("monthly")))]
    if facts.get("ma200"):
        rows += [("200-day avg", _price(facts["ma200"])), ("Price vs avg", f"{facts['price'] / facts['ma200'] - 1:+.1%}"),
                 ("Avg, 20 days", percent(facts.get("ma_slope")))]
    past = []
    if facts.get("drawdown_share") is not None:
        past.append(("Days this low", _share(facts["drawdown_share"])))
    if name == "SPY" and facts.get("rule_share") is not None:
        past.append(("Days in a dip", _share(facts["rule_share"])))
    return rows + _since(_year(facts.get("history_since")), *past)


def _vix_rows(signal: SubSignal) -> list[tuple]:
    m = signal.metrics
    closes = signal.history.vix if signal.history is not None else None
    rows = [("Now", f"{signal.score:.1f}")]
    if m.get("change") is not None:
        rows.append(("Today", percent(m["change"])))
    for label, sessions in (("1 week ago", 5), ("1 month ago", 21)):
        earlier = _earlier(closes, sessions)
        if earlier is not None:
            rows.append((label, f"{earlier:.1f}"))
    if m.get("recent_peak"):
        rows.append(("20-day high" if m.get("peak_basis") == "intraday" else "20-day close high", f"{m['recent_peak']:.1f}"))
    if m.get("usual_low") is not None:
        rows.append(("Usual range", f"{m['usual_low']:.1f}–{m['usual_high']:.1f}"))
    past = []
    if m.get("percentile") is not None:
        past.append(("Days below it", _share(m["percentile"])))
    if m.get("rule_share") is not None:
        past.append((f"Days at {VIX_SOFT:g}+", _share(m["rule_share"])))
    return rows + _since(_year(m.get("since")), *past)


def _credit_rows(signal: SubSignal) -> list[tuple]:
    m = signal.metrics
    now = signal.score * 100
    spreads = signal.history.spread if signal.history is not None else None
    rows = [("Now", f"{now:.0f} bp")]
    for label, sessions in (("1 week ago", 5), ("1 month ago", 21)):
        earlier = _earlier(spreads, sessions)
        if earlier is not None:
            rows.append((label, f"{earlier * 100:.0f} bp"))
    if m.get("stress") is not None:
        line = m["stress"] * 100
        rows += [("Stress line", f"{line:.0f} bp"), ("Below line by" if now < line else "Above line by", f"{abs(line - now):.0f} bp")]
    if m.get("usual_low") is not None:
        rows.append(("Usual range", f"{m['usual_low'] * 100:.0f}–{m['usual_high'] * 100:.0f} bp"))
    past = [("Days below it", _share(m["percentile"]))] if m.get("percentile") is not None else []
    return rows + _since(_year(m.get("since")), *past)


def _fed_table(signal: SubSignal) -> list[tuple]:
    frame = signal.history
    if frame is None or frame.empty:
        return []
    frame = frame.assign(meeting_date=pd.to_datetime(frame["meeting_date"])).sort_values("meeting_date")
    first = frame.iloc[0]
    low, high = first.get("reference_lower_bps"), first.get("reference_upper_bps")
    rows = [(f"Rate now {low / 100:.2f}–{high / 100:.2f}%",), ()] if pd.notna(low) and pd.notna(high) else []
    rows.append(("By", "Lower", "Same", "Higher"))
    rows += [(_day(row.meeting_date), f"{row.prob_ease:.0%}", f"{row.prob_no_change:.0%}", f"{row.prob_hike:.0%}")
             for row in frame.itertuples()]
    moved = frame[frame["delta_hike_pp"].notna()] if "delta_hike_pp" in frame else frame.iloc[0:0]
    if len(moved):
        since = moved["comparison_since"].dropna().iloc[0] if "comparison_since" in moved and moved["comparison_since"].notna().any() else None
        rows += [(), (f"Change since {pd.Timestamp(since):%-d %b}, points" if since is not None else "Change, points",)]
        rows += [(_day(row.meeting_date), f"{row.delta_ease_pp:+.0f}", f"{row.delta_no_change_pp:+.0f}", f"{row.delta_hike_pp:+.0f}")
                 for row in moved.itertuples()]
    return rows


def _margin_rows(signal: SubSignal) -> list[tuple]:
    m = signal.metrics
    rows = []
    if signal.history is not None and len(signal.history):
        frame = signal.history.assign(change=signal.history.debit_balances.pct_change()).tail(_HISTORY_ROWS).iloc[::-1]
        rows = [(f"{row.month:%b %Y}", f"${row.debit_balances / 1e6:.3f}T", "" if pd.isna(row.change) else f"{row.change:+.1%}")
                for row in frame.itertuples()]
    else:
        rows = [("Latest", f"${m.get('latest', 0) / 1e6:.3f}T", f"{signal.score:+.1%}")]
    if m.get("yearly") is not None:
        rows.append(("In a year", "", f"{m['yearly']:+.1%}"))
    if m.get("falling_months", 0) > 1:
        rows.append(("Falls in a row", "", str(m["falling_months"])))
    past = [("Months falling", "", _share(m["falling_share"]))] if m.get("falling_share") is not None else []
    return rows + _since(_year(m.get("since")), *past)


def _sector_table(signal: SubSignal) -> tuple[list[tuple], list[str]]:
    """(table rows, warnings): forecast growth and estimate coverage for each industry."""
    frame = signal.history
    if frame is None or frame.empty:
        return [], []
    covered = {"covered_companies", "sampled_companies"}.issubset(frame.columns)
    rows, warnings = [("Industry", "Growth", "Firms") if covered else ("Industry", "Growth")], []
    for row in frame.itertuples():
        name = str(row.industry)
        name = name if len(name) <= 16 else name[:15] + "…"
        growth = "n/a" if pd.isna(row.earnings_growth_estimate_1y) else f"{row.earnings_growth_estimate_1y:+.0%}"
        if not covered:
            rows.append((name, growth))
            continue
        rows.append((name, growth, f"{int(row.covered_companies)}/{int(row.sampled_companies)}"))
        if getattr(row, "estimate_coverage", 1.) < SECTOR_MIN_ESTIMATE_COVERAGE:
            warnings.append(f"⚠️ Too few estimates: {row.industry} ({int(row.covered_companies)} of {int(row.sampled_companies)} firms)")
    low, high = frame.get("analysts_min"), frame.get("analysts_max")
    if low is not None and high is not None and low.notna().any():
        rows += [(), (f"Analysts per firm {int(low.min())}–{int(high.max())}",)]
    return rows, warnings


def _curve_rows(signal: SubSignal) -> list[tuple]:
    m = signal.metrics
    rows = [("10y − 3m", f"{signal.score:+.2f} pp")]
    if m.get("policy_spread") is not None:
        rows.append(("2y − Fed rate", f"{m['policy_spread']:+.2f} pp"))
    if m.get("cuts_priced") is not None:
        since = f"since {pd.Timestamp(m['cuts_since']):%-d %b}" if m.get("cuts_since") else "yes"
        rows.append(("Cuts priced in", since if m["cuts_priced"] else "no"))
    buckets = m.get("buckets") or []
    if buckets:
        policy = any(bucket.get("policy_spread") is not None for bucket in buckets)
        rows += [(), ("3-week avg to", "10y−3m", "2y−Fed") if policy else ("3-week avg to", "10y−3m")]
        for bucket in buckets:
            label = f"{pd.Timestamp(bucket['thru']):%-d %b}" + ("*" if bucket.get("partial") else "")
            cells = [label, f"{bucket['spread']:+.2f}"]
            if policy:
                cells.append("n/a" if bucket.get("policy_spread") is None else f"{bucket['policy_spread']:+.2f}")
            rows.append(tuple(cells))
        if any(bucket.get("partial") for bucket in buckets):
            rows.append(("* still filling",))
    past = [("Days inverted", _share(m["inverted_share"]))] if m.get("inverted_share") is not None else []
    return rows + _since(_year(m.get("since")), *past)


_ROWS = {"vix": _vix_rows, "credit": _credit_rows, "fed_rate": _fed_table,
         "margin_debt": _margin_rows, "yield_curve": _curve_rows}


def _data_line(signal: SubSignal) -> str:
    if signal.name == "sector":
        retrieved = signal.history.fetched_at.iloc[-1] if signal.history is not None and "fetched_at" in signal.history else None
        return "Forecasts as of " + stamp(retrieved)
    if signal.name == "margin_debt":
        return "Data " + (pd.Timestamp(signal.observed_at).strftime("%b %Y") if signal.observed_at else "—")
    return "Data " + stamp(signal.observed_at)


def panel_caption(signal: SubSignal, view: str, sent_at: str | None = None):
    base, _ = split_view(view)
    if signal.name == "market_dip":
        name = base.removeprefix("index_") if base and base.startswith("index_") else "SPY"
        title = f"<b>{esc(INDEX_NAMES.get(name, name))}</b>"
        rows = _market_rows(signal, name)
        if not rows:
            return f"{title}\nNot enough history for this index."
        lines = [title + (f" · {verdict(signal)}" if name == "SPY" else ""), _pre(_lines(rows))]
        lines.append("Data " + stamp(_index_facts(signal, name).get("observed_at") or signal.observed_at))
        if signal.metrics.get("stale"):
            lines.append("⚠️ Prices are not from the latest session")
        return "\n".join(lines)
    lines = [f"<b>{esc(TITLES.get(signal.name, signal.name))}</b> · {esc(verdict(signal))}"]
    rows, warnings = _sector_table(signal) if signal.name == "sector" else (_ROWS[signal.name](signal), [])
    if rows:
        lines.append(_pre(_lines(rows)))
    lines.extend(esc(warning) for warning in warnings)
    lines.append(_data_line(signal))
    return "\n".join(lines)


def caption_pages(text, limit=950):
    """Captions are written to fit one photo caption; an oversized one is cut on a line break."""
    from buysignal.telegram_bot.html_chunks import split_html
    if len(text.encode("utf-16-le")) // 2 <= limit:
        return [text]
    return split_html(text, limit=limit)[:1]
