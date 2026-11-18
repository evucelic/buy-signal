"""Overview and panel captions: every number behind a reading, laid out to scan; no collection or IO.

Status sits in emoji the eye finds first (✅ ❌ ❔ for the checklist, 🟢 🟠 for context), names
in bold, numbers in monospace tables of about 32 characters so a phone does not wrap them. A panel's
numbers follow its range button: the table describes the same stretch of history as the chart.
Warnings for missing or stale data stay in the message.
"""
from __future__ import annotations

from datetime import datetime
import os
from zoneinfo import ZoneInfo

import pandas as pd

from buysignal.analysis.signal_charts import DEFAULT_RANGE, RANGES
from buysignal.config import MARKET_DIP_THRESHOLD, MARKET_DRAWDOWN_SESSIONS, SECTOR_MIN_ESTIMATE_COVERAGE, VIX_SOFT, VIX_STRONG
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
_ICONS = {"vix": "😨", "fed_rate": "🏛", "margin_debt": "💳", "market_dip": "📉", "sector": "💹",
          "credit": "🏦", "yield_curve": "〰️"}
# One line for what a measure is, where the name alone does not say it.
GLOSSES = {
    "fed_rate": "CME futures odds of where the Fed's rate will be on each date, against today's range.",
    "margin_debt": "Money investors borrow from brokers to buy shares. FINRA, monthly, about 3 weeks late.",
    "market_dip": f"Dip: {-MARKET_DIP_THRESHOLD:.0%} or more below the highest close of the prior {MARKET_DRAWDOWN_SESSIONS} sessions.",
    "sector": "Analysts' growth forecast for next year's earnings, largest firms of the 5 biggest industries.",
    "credit": ("Extra yield Baa-rated companies pay over the 10-year Treasury. It widens when lenders fear defaults. "
               "Stress line: the spread was wider only 1 day in 5 over the last 10 years."),
    "yield_curve": "Long minus short Treasury yield. Below 0 (inverted) has come before most US recessions.",
}
# The /portfolio tabs share the panel machinery; their names are listed here so buttons validate.
PORTFOLIO_VIEWS = {"pf_overview", "pf_returns", "pf_mix", "pf_risk", "pf_entries", "pf_fees", "pf_cash", "pf_pe"}
VIEWS = {name for name, _ in SECTIONS} | {"close"} | {"index_" + name for name in INDEXES} | PORTFOLIO_VIEWS
_SELECTED = "● "
_HISTORY_ROWS = 6  # recent observations listed in a monthly panel

# Checklist rows: the condition's short name, and its mark for pass, fail and no current data.
_CHECK_NAMES = {"vix": "Fear", "fed_rate": "Fed", "margin_debt": "Margin", "market_dip": "Dip", "sector": "EPS"}
_MARKS = {"pass": "✅", "fail": "❌", "incomplete": "❔"}
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
    return base in VIEWS and (not chosen or chosen in RANGES)


def section_of(base: str) -> str:
    return "market_dip" if base.startswith("index_") else base


# --- formatting --------------------------------------------------------------------------------


def _share(value: float) -> str:
    # Never round a rare reading to "0%" or a common one to "100%".
    if value <= 0:
        return "0%"
    if value < .01:
        return "under 1%"
    if value >= 1:
        return "100%"
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


def _day(value) -> str:
    """'9 Dec 26' with the day padded, so a column of dates lines up."""
    value = pd.Timestamp(value)
    return f"{value.day:>2} {value:%b %y}"


# --- what each reading says --------------------------------------------------------------------


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
    return signal.metrics.get("index_facts", {}).get(name) or {}


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


def _mark(signal: SubSignal) -> str:
    """✅/❌ for a checklist condition, 🟢/🟠 for context: whether the reading is in the owner's favour."""
    if signal.advisory or signal.name not in REQUIRED_SIGNALS:
        if signal.name == "yield_curve":
            return "🟠" if signal.state in ("inverted", "deep_inversion") else "🟢"
        return "🟢" if signal.passes else "🟠"
    return _MARKS["pass" if signal.passes else "fail"]


# --- overview ----------------------------------------------------------------------------------


def _check_value(signal: SubSignal) -> str:
    """The number behind one checklist condition, in a few words."""
    m = signal.metrics
    if signal.name == "vix":
        return f"VIX {signal.score:.1f}" + (" · high fear" if signal.score >= VIX_STRONG else "")
    if signal.name == "fed_rate":
        rows = _fed_rows(signal)
        if not rows:
            return "odds unavailable"
        date, label, odds = next((row for row in rows if row[1] == "higher"), rows[-1])
        return f"{label} leads {odds:.0%} by {date:%b %y}"
    if signal.name == "margin_debt":
        month = pd.Timestamp(signal.observed_at).strftime("%b") if signal.observed_at else "latest"
        return f"{signal.score:+.1%} in {month}"
    if signal.name == "market_dip":
        return f"SPY {signal.score:+.1%} from 3-month high"
    if signal.name == "sector":
        return f"{m.get('growing', '—')} of {m.get('sample_size', '—')} industries growing"
    return f"{signal.score:+.2f}"


def _meter(passed: int, total: int) -> str:
    return "●" * passed + "○" * (total - passed)


def _context_line(signal: SubSignal | None, label: str) -> str:
    if signal is None:
        return f"❔ <b>{label}</b> no data"
    if signal.name == "credit":
        m = signal.metrics
        value = f"{signal.score * 100:.0f} bp"
        if m.get("stress") is not None:
            value += f" · stress line {m['stress'] * 100:.0f}"
    else:
        value = f"{signal.score:+.2f} pp · {verdict(signal)}"
    return f"{_mark(signal)} <b>{label}</b> {esc(value)}"


def _sources(by_name: dict, caches, now: datetime) -> str:
    refreshed = dict(caches)
    labels = (("market_dip", "Prices"), ("vix", "VIX"), ("fed_rate", "Fed"), ("margin_debt", "Margin"),
              ("sector", "Earnings"), ("credit", "Credit"), ("yield_curve", "Curve"))
    lines = ["🕒 <b>Data as of</b> · (last refresh)"]
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
    passed = sum(result.status(name) == "pass" for name in REQUIRED_SIGNALS)
    lines = [f"📊 <b>Entry checklist · {esc(result.label)}</b>", _meter(passed, len(REQUIRED_SIGNALS)), ""]
    for name in REQUIRED_SIGNALS:
        signal, status = by_name.get(name), result.status(name)
        # ❔ marks a reading that is missing or stale; the warning below names it.
        value = _check_value(signal) if signal is not None else "no data"
        lines.append(f"{_MARKS[status]} <b>{_CHECK_NAMES[name]}</b> {esc(value)}")
    lines += ["", "🧭 <b>Context</b>", _context_line(by_name.get("credit"), "Credit"),
              _context_line(by_name.get("yield_curve"), "Curve")]
    market = by_name.get("market_dip")
    changes = market.metrics.get("index_changes", {}) if market else {}
    if changes:
        rows = [("", "Day", "Week", "Month")]
        rows += [(_INDEX_BUTTONS.get(name, name), *[f"{value:+.1%}" for value in item["changes"]]) for name, item in changes.items()]
        lines += ["", "🌍 <b>Markets</b>", _pre(_lines(rows))]
    if result.state == INCOMPLETE:
        unknown = [_CHECK_NAMES[name] for name in REQUIRED_SIGNALS if not usable(by_name.get(name))]
        lines.append("⚠️ No current data: " + esc(", ".join(unknown)))
    if market:
        m = market.metrics
        if m.get("stale"):
            lines.append(f"⚠️ Prices cached through {esc(market.observed_at or 'unknown')}; expected {esc(m.get('expected_session') or 'latest session')}.")
        elif m.get("refresh_failed"):
            lines.append("⚠️ Price refresh failed; showing cached observations.")
        if len({item["date"] for item in changes.values()}) > 1:
            lines.append("Index dates: " + esc(" · ".join(f"{name} {item['date']}" for name, item in changes.items())))
    lines.append(_sources(by_name, caches, now))
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


# --- panels: the numbers behind each reading, over the range on screen ------------------------


def _measure(signal: SubSignal, name: str = "") -> pd.Series | None:
    """The plotted measure as a date-indexed series, in the unit the panel shows."""
    frame = signal.history
    if frame is None or frame.empty:
        return None
    if signal.name == "market_dip":
        return frame[name].dropna() if name in frame else None
    column, dates, scale = {"vix": ("vix", "date", 1), "credit": ("spread", "date", 100),
                            "yield_curve": ("spread", "date", 1), "margin_debt": ("debit_balances", "month", 1)}.get(signal.name, (None,) * 3)
    if column not in frame or dates not in frame:
        return None
    series = pd.Series(pd.to_numeric(frame[column], errors="coerce").to_numpy() * scale, index=pd.to_datetime(frame[dates]))
    return series.dropna().sort_index()


def _window(series: pd.Series, range_key: str, monthly: bool = False) -> pd.Series:
    size = RANGES[range_key]["months" if monthly else "sessions"]
    if size is None and range_key != "max":
        size = 3  # a monthly series has no 3-month button; never let one show all history
    return series if size is None else series.tail(size + 1)


def _span(series: pd.Series, range_key: str) -> str:
    """'past year', 'past 5 years', or 'since 2016' for the whole history."""
    words = RANGES[range_key]["words"]
    if words is None:
        return f"since {series.index[0].year}"
    return "past " + words.removeprefix("1 ")


def _ago(range_key: str, series: pd.Series) -> str:
    words = RANGES[range_key]["words"]
    return f"{series.index[0]:%b %Y}" if words is None else words.replace("months", "mo").replace("years", "yrs").replace("year", "yr") + " ago"


def _extremes(window: pd.Series, show) -> list[tuple]:
    high, low = window.idxmax(), window.idxmin()
    return [("High", show(window[high]), _day(high).strip()), ("Low", show(window[low]), _day(low).strip()),
            ("Average", show(window.mean()), "")]


def _range_block(window: pd.Series, range_key: str, show, change) -> list[tuple]:
    """Where the range started, what changed, and its high, low and average."""
    return [(_ago(range_key, window), show(window.iloc[0]), ""), ("Change", change(window.iloc[-1], window.iloc[0]), ""),
            *_extremes(window, show)]


def _story(window: pd.Series, range_key: str, *facts: str) -> list[str]:
    """One line on how today compares with the range on screen: 'Past year · wider on 43% of days'."""
    span = _span(window, range_key)
    return ["📏 " + esc(" · ".join([span[0].upper() + span[1:], *facts]))]


def _vix_panel(signal: SubSignal, range_key: str) -> tuple[str, list[tuple], list[str]]:
    m = signal.metrics
    head = f"<b>{signal.score:.1f}</b>" + (f" · {esc(percent(m['change']))} today" if m.get("change") is not None else "")
    series = _measure(signal)
    if series is None or len(series) < 2:
        return head, [], []
    window = _window(series, range_key)
    rows = _range_block(window, range_key, lambda v: f"{v:.1f}", lambda a, b: f"{a - b:+.1f}")
    if m.get("recent_peak"):
        rows.append(("20-day peak", f"{m['recent_peak']:.1f}", ""))
    higher = float((window.iloc[:-1] > signal.score).mean())
    fear = float((window >= VIX_SOFT).mean())
    stories = _story(window, range_key, f"higher on {_share(higher)} of days", f"fear ({VIX_SOFT:g}+) on {_share(fear)}")
    return head, rows, stories


def _credit_panel(signal: SubSignal, range_key: str) -> tuple[str, list[tuple], list[str]]:
    m = signal.metrics
    now = signal.score * 100
    head = f"<b>{now:.0f} bp</b> = {signal.score:.2f}% a year extra"
    series = _measure(signal)
    rows, stories = [], []
    line = m["stress"] * 100 if m.get("stress") is not None else None
    if line is not None:
        rows += [("Stress line", f"{line:.0f} bp", ""),
                 ("To the line" if now < line else "Over the line", f"{abs(line - now):.0f} bp", "")]
    if series is not None and len(series) > 1:
        window = _window(series, range_key)
        rows += _range_block(window, range_key, lambda v: f"{v:.0f} bp", lambda a, b: f"{a - b:+.0f} bp")
        facts = [f"wider on {_share(float((window.iloc[:-1] > now).mean()))} of days"]
        if line is not None:
            facts.append(f"over the line on {_share(float((window >= line).mean()))}")
        stories = _story(window, range_key, *facts)
    return head, rows, stories


def _curve_panel(signal: SubSignal, range_key: str) -> tuple[str, list[tuple], list[str]]:
    m = signal.metrics
    head = f"<b>{signal.score:+.2f} pp</b> 10-year minus 3-month"
    rows = []
    if m.get("policy_spread") is not None:
        rows.append(("2y − Fed rate", f"{m['policy_spread']:+.2f}", "cuts priced" if m.get("cuts_priced") else ""))
    series = _measure(signal)
    stories = []
    if series is not None and len(series) > 1:
        window = _window(series, range_key)
        rows += _range_block(window, range_key, lambda v: f"{v:+.2f}", lambda a, b: f"{a - b:+.2f}")
        stories = _story(window, range_key, f"inverted on {_share(float((window < 0).mean()))} of days")
    return head, rows, stories


def _margin_panel(signal: SubSignal, range_key: str) -> tuple[str, list[tuple], list[str]]:
    m = signal.metrics
    head = f"<b>${m.get('latest', 0) / 1e6:.3f}T</b> · {esc(percent(signal.score))} in a month"
    series = _measure(signal)
    if series is None or len(series) < 2:
        return head, [], []
    window = _window(series, range_key, monthly=True)
    show = lambda v: f"${v / 1e6:.3f}T"  # noqa: E731
    recent = series.pct_change().tail(_HISTORY_ROWS).iloc[::-1]
    rows = [(f"{month:%b %Y}", show(series[month]), "" if pd.isna(change) else f"{change:+.1%}") for month, change in recent.items()]
    peak = window.idxmax()
    rows += [(), (_ago(range_key, window), show(window.iloc[0]), f"{window.iloc[-1] / window.iloc[0] - 1:+.1%}"),
             ("Peak", show(window[peak]), f"{peak:%b %y}"), ("From peak", "", f"{window.iloc[-1] / window[peak] - 1:+.1%}")]
    changes = window.pct_change().dropna()
    facts = [f"fell in {int((changes < 0).sum())} of {len(changes)} months"]
    if m.get("falling_months", 0) > 1:
        facts.append(f"{m['falling_months']} in a row now")
    stories = _story(window, range_key, *facts)
    return head, rows, stories


def _market_panel(signal: SubSignal, name: str, range_key: str) -> tuple[str, list[tuple], list[str]]:
    facts = _index_facts(signal, name)
    if not facts:
        return "", [], []
    head = f"<b>{_price(facts['price'])}</b> · {esc(percent(facts.get('daily')))} today"
    rows = []
    if facts.get("peak") is not None:
        when = f"{pd.Timestamp(facts['peak_date']):%-d %b}" if facts.get("peak_date") else ""
        rows += [("3-mo high", _price(facts["peak"]), when), ("From high", f"{facts.get('drawdown', 0.):+.1%}", "")]
    if facts.get("ma200"):
        rows.append(("vs 200-day", f"{facts['price'] / facts['ma200'] - 1:+.1%}", ""))
    series = _measure(signal, name)
    stories = []
    if series is not None and len(series) > 1:
        window = _window(series, range_key)
        rows += [()] + _range_block(window, range_key, _price, lambda a, b: f"{a / b - 1:+.1%}")
        drawdowns = (series / series.shift(1).rolling(MARKET_DRAWDOWN_SESSIONS).max() - 1).clip(upper=0).reindex(window.index).dropna()
        told = []
        if len(drawdowns) and facts.get("drawdown", 0) < 0:
            told.append(f"this far off the high on {_share(float((drawdowns.iloc[:-1] <= facts['drawdown']).mean()))} of days")
        if len(drawdowns):
            told.append(f"in a dip on {_share(float((drawdowns <= MARKET_DIP_THRESHOLD + 1e-12).mean()))}" + ("" if told else " of days"))
        stories = _story(window, range_key, *told) if told else []
    return head, rows, stories


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


_PANELS = {"vix": _vix_panel, "credit": _credit_panel, "yield_curve": _curve_panel, "margin_debt": _margin_panel}


def _data_line(signal: SubSignal) -> str:
    if signal.name == "sector":
        retrieved = signal.history.fetched_at.iloc[-1] if signal.history is not None and "fetched_at" in signal.history else None
        return "Forecasts as of " + stamp(retrieved)
    if signal.name == "margin_debt":
        return "Data " + (pd.Timestamp(signal.observed_at).strftime("%b %Y") if signal.observed_at else "—")
    return "Data " + stamp(signal.observed_at)


def _gloss(name: str) -> str:
    return f"<i>{esc(GLOSSES[name])}</i>" if name in GLOSSES else ""


def panel_caption(signal: SubSignal, view: str, sent_at: str | None = None, range_key: str | None = None):
    """The panel's text; for a ranged reading its numbers describe `range_key` (by default the view's)."""
    base, chosen = split_view(view)
    range_key = range_key if range_key in RANGES else chosen
    if signal.name == "market_dip":
        name = base.removeprefix("index_") if base and base.startswith("index_") else "SPY"
        head, rows, stories = _market_panel(signal, name, range_key)
        title = f"{_ICONS['market_dip']} <b>{esc(INDEX_NAMES.get(name, name))}</b>"
        if not head:
            return f"{title}\nNot enough history for this index."
        if name == "SPY":
            title += f" · {_mark(signal)} {verdict(signal)}"
        lines = [title, head, _pre(_lines(rows, notes=True)), *stories]
        if name == "SPY":
            lines.append(_gloss("market_dip"))
        lines.append("Data " + stamp(_index_facts(signal, name).get("observed_at") or signal.observed_at))
        if signal.metrics.get("stale"):
            lines.append("⚠️ Prices are not from the latest session")
        return "\n".join(line for line in lines if line)
    title = f"{_ICONS.get(signal.name, '')} <b>{esc(TITLES.get(signal.name, signal.name))}</b> · {_mark(signal)} {esc(verdict(signal))}"
    lines = [title]
    if signal.name in _PANELS:
        head, rows, stories = _PANELS[signal.name](signal, range_key)
        lines.append(head)
        if rows:
            lines.append(_pre(_lines(rows, notes=True)))
        lines += stories
        warnings = []
    elif signal.name == "sector":
        rows, warnings = _sector_table(signal)
        if rows:
            lines.append(_pre(_lines(rows)))
    else:
        rows, warnings = _fed_table(signal), []
        if rows:
            lines.append(_pre(_lines(rows)))
    lines.extend(esc(warning) for warning in warnings)
    lines.append(_gloss(signal.name))
    lines.append(_data_line(signal))
    return "\n".join(line for line in lines if line)


def caption_pages(text, limit=950):
    """Captions are written to fit one photo caption; an oversized one is cut on a line break."""
    from buysignal.telegram_bot.html_chunks import split_html
    if len(text.encode("utf-16-le")) // 2 <= limit:
        return [text]
    return split_html(text, limit=limit)[:1]
