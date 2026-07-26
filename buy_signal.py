"""Combine the sub-signals into a checklist-style alert."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from signals import margin_signal, market_signal, rate_signal, sector_signal, vix_signal
from signals.base import SubSignal

# compute_signal() drives collectors that read-modify-write shared CSV caches; serialize
# concurrent callers (e.g. the background tick loop and an on-demand Telegram /signal) so
# they don't race on the same file.
_lock = threading.Lock()


@dataclass
class BuySignal:
    score: float
    state: str
    passing_count: int
    subsignals: list[SubSignal]
    missing_signals: list[str] = field(default_factory=list)
    detail: str = ""


def _alert_state(subsignals: list[SubSignal], missing_signals: list[str]) -> tuple[str, str]:
    if not subsignals or any(not signal.passes for signal in subsignals):
        return "none", "NONE: one or more required signals failed."
    if missing_signals:
        return "soft", "SOFT: required signals passed, but some signals are missing."
    return "strong", "STRONG: all required signals passed."


def compute_signal(vix: float | None = None) -> BuySignal:
    """Score the available signals and combine them with checklist rules."""
    with _lock:
        return _compute_signal(vix)


def _compute_signal(vix: float | None) -> BuySignal:
    subsignals: list[SubSignal] = []
    missing_signals: list[str] = []

    try:
        subsignals.append(vix_signal.score(vix))
    except NotImplementedError:
        missing_signals.append("vix")
    except Exception as exc:
        missing_signals.append("vix")
        print(f"signal skipped (vix: {type(exc).__name__}: {exc})")

    try:
        subsignals.append(rate_signal.score())
    except NotImplementedError:
        missing_signals.append("fed_rate")
    except Exception as exc:
        missing_signals.append("fed_rate")
        print(f"signal skipped (fed_rate: {type(exc).__name__}: {exc})")

    try:
        subsignals.append(margin_signal.score())
    except NotImplementedError:
        missing_signals.append("margin_debt")
    except Exception as exc:
        missing_signals.append("margin_debt")
        print(f"signal skipped (margin_debt: {type(exc).__name__}: {exc})")

    try:
        subsignals.append(market_signal.score())
    except NotImplementedError:
        missing_signals.append("market_dip")
    except Exception as exc:
        missing_signals.append("market_dip")
        print(f"signal skipped (market_dip: {type(exc).__name__}: {exc})")

    try:
        subsignals.append(sector_signal.score())
    except NotImplementedError:
        missing_signals.append("sector")
    except Exception as exc:
        missing_signals.append("sector")
        print(f"signal skipped (sector: {type(exc).__name__}: {exc})")

    passing_count = sum(1 for signal in subsignals if signal.passes)
    score = passing_count / len(subsignals) if subsignals else 0.0
    state, summary = _alert_state(subsignals, missing_signals)

    return BuySignal(
        score=score,
        state=state,
        passing_count=passing_count,
        subsignals=subsignals,
        missing_signals=sorted(set(missing_signals)),
        detail=summary,
    )


if __name__ == "__main__":
    result = compute_signal()
    print(f"Alert: {result.state.upper()}  (passing {result.passing_count}/{len(result.subsignals)}; score {result.score:+.2f})")
    print(f"Rule: {result.detail}")
    if result.missing_signals:
        print(f"Missing: {', '.join(result.missing_signals)}")
    for signal in result.subsignals:
        status = "PASS" if signal.passes else "FAIL"
        print(f"  - {signal.name:12s} {status:4s} {signal.state:11s} {signal.detail}")
