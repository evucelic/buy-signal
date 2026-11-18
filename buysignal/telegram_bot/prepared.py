"""Plan a report's panels once: captions, keyboards and chart jobs.

Each section's default chart is drawn before the report is sent; the other ranges follow in the background.
"""
from dataclasses import dataclass, replace
import logging

from buysignal import config
from buysignal.analysis import signal_charts
from buysignal.analysis.signal_charts import DEFAULT_RANGE
from buysignal.signals.base import SubSignal
from buysignal.telegram_bot import views

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChartJob:
    key: str
    signal: SubSignal
    range_key: str
    primary: bool


def chart_key(name: str, index: str = "", range_key: str = DEFAULT_RANGE) -> str:
    return f"{name}:{index}" + ("" if range_key == DEFAULT_RANGE else "@" + range_key)


def plan(result, token, sent_at):
    """Return ({view: {caption, keyboard, chart_key}}, [ChartJob]) for every reachable view."""
    signals = {s.name: s for s in result.subsignals}
    captions, jobs = {}, {}
    available = []
    market = signals.get("market_dip")
    if market is not None and market.history is not None:
        available = [k for k in views.INDEXES if k in market.history and market.history[k].notna().any()]
    for name, _ in views.SECTIONS:
        signal = signals.get(name)
        if signal is None:
            continue
        targets = [(("market_dip" if index == "SPY" else "index_" + index), index) for index in available] if name == "market_dip" else [(name, "")]
        for base, index in targets:
            shown = signal
            if name == "market_dip":
                observed = str(signal.history[index].dropna().index[-1].date())
                shown = replace(signal, observed_at=observed, metrics={**signal.metrics, "chart_index": index})
            ranges = signal_charts.available_ranges(shown, index or None) if shown.history is not None else ()
            for range_key in ranges or (DEFAULT_RANGE,):
                view = views.view_name(base, range_key)
                # The numbers follow the range button, like the chart above them.
                caption = views.caption_pages(views.panel_caption(shown, base, sent_at, range_key))[0]
                key = chart_key(name, index, range_key)
                if shown.history is not None and key not in jobs:
                    jobs[key] = ChartJob(key, shown, range_key, range_key == DEFAULT_RANGE and index in ("", "SPY"))
                captions[view] = {
                    "caption": caption,
                    "keyboard": views.keyboard(token, view, available, sections=set(signals), ranges=ranges),
                    "chart_key": key if shown.history is not None else None,
                }
    return captions, list(jobs.values())


def render(job: ChartJob) -> bytes | None:
    """One chart, or None when charts are disabled or this one cannot be drawn."""
    if not config.TELEGRAM_CHARTS:
        return None
    try:
        return signal_charts.render_signal_chart(job.signal, job.range_key)
    except Exception as exc:
        log.warning("Chart preparation failed for %s (%s)", job.key, type(exc).__name__)
        return None


def background_order(job: ChartJob) -> tuple:
    """Draw the default section's other ranges before other indexes, and their ranges last."""
    other_index = job.signal.name == "market_dip" and job.signal.metrics.get("chart_index", "SPY") != "SPY"
    return (other_index and job.range_key != DEFAULT_RANGE, other_index, job.signal.name != "market_dip")
