"""Combine the sub-signals into a checklist-style alert."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from signals import margin_signal, market_signal, rate_signal, sector_signal, vix_signal, yield_curve_signal
from signals.base import SubSignal

# compute_signal() (and any individual signal's score()) drives collectors that read-modify-
# write shared CSV caches; serialize concurrent callers (e.g. the background tick loop and an
# on-demand Telegram command) so they don't race on the same file.
SIGNAL_LOCK = threading.Lock()

# Advisory signals are context only: they never gate the checklist, and one going missing
# must not downgrade strong -> soft the way a missing required signal does.
_ADVISORY_SIGNALS = {"yield_curve"}


@dataclass
class BuySignal:
    score: float
    state: str
    passing_count: int
    subsignals: list[SubSignal]
    missing_signals: list[str] = field(default_factory=list)
    detail: str = ""

    @property
    def required_count(self) -> int:
        """Checklist size: advisory subsignals are shown but don't count as conditions."""
        return sum(1 for signal in self.subsignals if not signal.advisory)


def _alert_state(required: list[SubSignal], missing_signals: list[str]) -> tuple[str, str]:
    if not required or any(not signal.passes for signal in required):
        return "none", "NONE: one or more required signals failed."
    if missing_signals:
        return "soft", "SOFT: required signals passed, but some signals are missing."
    return "strong", "STRONG: all required signals passed."


def compute_signal(vix: float | None = None, allow_refresh: bool = True) -> BuySignal:
    """Score the available signals and combine them with checklist rules.

    allow_refresh=False tells VIX/market_dip (the two that refetch every call) to prefer
    their cache instead, for on-demand checks while the market's closed and there's nothing
    new to find.
    """
    with SIGNAL_LOCK:
        return _compute_signal(vix, allow_refresh)


def _compute_signal(vix: float | None, allow_refresh: bool) -> BuySignal:
    subsignals: list[SubSignal] = []
    missing_signals: list[str] = []

    try:
        subsignals.append(vix_signal.score(vix, allow_refresh=allow_refresh))
    except Exception as exc:
        missing_signals.append("vix")
        print(f"signal skipped (vix: {type(exc).__name__}: {exc})")

    try:
        subsignals.append(rate_signal.score())
    except Exception as exc:
        missing_signals.append("fed_rate")
        print(f"signal skipped (fed_rate: {type(exc).__name__}: {exc})")

    try:
        subsignals.append(margin_signal.score())
    except Exception as exc:
        missing_signals.append("margin_debt")
        print(f"signal skipped (margin_debt: {type(exc).__name__}: {exc})")

    try:
        subsignals.append(market_signal.score(allow_refresh=allow_refresh))
    except Exception as exc:
        missing_signals.append("market_dip")
        print(f"signal skipped (market_dip: {type(exc).__name__}: {exc})")

    try:
        subsignals.append(sector_signal.score())
    except Exception as exc:
        missing_signals.append("sector")
        print(f"signal skipped (sector: {type(exc).__name__}: {exc})")

    try:
        subsignals.append(yield_curve_signal.score())
    except Exception as exc:
        missing_signals.append("yield_curve")
        print(f"signal skipped (yield_curve: {type(exc).__name__}: {exc})")

    required = [signal for signal in subsignals if not signal.advisory]
    required_missing = [name for name in missing_signals if name not in _ADVISORY_SIGNALS]
    passing_count = sum(1 for signal in required if signal.passes)
    score = passing_count / len(required) if required else 0.0
    state, summary = _alert_state(required, required_missing)

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
    print(f"Alert: {result.state.upper()}  (passing {result.passing_count}/{result.required_count}; score {result.score:+.2f})")
    print(f"Rule: {result.detail}")
    if result.missing_signals:
        print(f"Missing: {', '.join(result.missing_signals)}")
    for signal in result.subsignals:
        status = "PASS" if signal.passes else "FAIL"
        print(f"  - {signal.name:12s} {status:4s} {signal.state:11s} {signal.detail}")
