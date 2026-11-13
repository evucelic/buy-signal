"""Combine the sub-signals into the five-condition entry checklist.

Five conditions are required: fear (VIX), the Fed path, margin deleveraging, the SPY dip and
forecast earnings. The headline is how many pass, out of five. A required reading that is
missing or stale cannot pass and keeps its place in the denominator, so the read is marked
incomplete rather than quietly shrinking to "3 of 4". Credit and the yield curve are context:
shown beside the checklist, never counted.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field

from buysignal.signals import credit_signal, margin_signal, market_signal, rate_signal, sector_signal, vix_signal, yield_curve_signal
from buysignal.signals.base import SubSignal

log = logging.getLogger(__name__)

# compute_signal() (and any individual signal's score()) drives collectors that read-modify-
# write shared CSV caches; serialize concurrent callers (e.g. the background tick loop and an
# on-demand Telegram command) so they don't race on the same file.
SIGNAL_LOCK = threading.Lock()

REQUIRED_SIGNALS = ("vix", "fed_rate", "margin_debt", "market_dip", "sector")
CONTEXT_SIGNALS = ("credit", "yield_curve")
INCOMPLETE = "incomplete"
PASS, FAIL = "pass", "fail"


@dataclass
class BuySignal:
    state: str            # "strong" (all five pass), "none" (some fail) or "incomplete"
    passing_count: int
    subsignals: list[SubSignal]
    missing_signals: list[str] = field(default_factory=list)
    detail: str = ""

    @property
    def required_count(self) -> int:
        return len(REQUIRED_SIGNALS)

    @property
    def label(self) -> str:
        return f"{self.passing_count} of {self.required_count}" + (" incomplete" if self.state == INCOMPLETE else "")

    def status(self, name: str) -> str:
        """'pass', 'fail' or 'incomplete' for one required condition."""
        signal = next((s for s in self.subsignals if s.name == name), None)
        if not usable(signal):
            return INCOMPLETE
        return PASS if signal.passes else FAIL


def usable(signal: SubSignal | None) -> bool:
    """Present and current: a stale reading cannot pass a condition."""
    return signal is not None and not signal.metrics.get("stale")


def assess(subsignals: list[SubSignal], missing_signals=()) -> BuySignal:
    """Count the required conditions that pass; any missing or stale one makes the read incomplete."""
    by_name = {signal.name: signal for signal in subsignals}
    unknown = [name for name in REQUIRED_SIGNALS if not usable(by_name.get(name))]
    passing = [name for name in REQUIRED_SIGNALS if name not in unknown and by_name[name].passes]
    if unknown:
        state, detail = INCOMPLETE, "Incomplete: no current " + ", ".join(unknown)
    elif len(passing) == len(REQUIRED_SIGNALS):
        state, detail = "strong", "All five conditions pass"
    else:
        failing = [name for name in REQUIRED_SIGNALS if name not in passing]
        state, detail = "none", "Not passing: " + ", ".join(failing)
    return BuySignal(state, len(passing), list(subsignals), sorted(set(missing_signals)), detail)


def compute_signal(vix: float | None = None, allow_refresh: bool = True) -> BuySignal:
    """Score the available signals and count the checklist.

    allow_refresh=False tells VIX/market_dip (the two that refetch every call) to prefer
    their cache instead, for on-demand checks while the market's closed and there's nothing
    new to find.
    """
    with SIGNAL_LOCK:
        return _compute_signal(vix, allow_refresh)


def _compute_signal(vix: float | None, allow_refresh: bool) -> BuySignal:
    subsignals: list[SubSignal] = []
    missing_signals: list[str] = []
    scorers = (
        ("vix", lambda: vix_signal.score(vix, allow_refresh=allow_refresh)),
        ("fed_rate", rate_signal.score),
        ("margin_debt", margin_signal.score),
        ("market_dip", lambda: market_signal.score(allow_refresh=allow_refresh)),
        ("sector", sector_signal.score),
        ("credit", credit_signal.score),
        ("yield_curve", yield_curve_signal.score),
    )
    for name, score in scorers:
        try:
            subsignals.append(score())
        except Exception as exc:
            missing_signals.append(name)
            log.warning("signal skipped (%s: %s: %s)", name, type(exc).__name__, exc)
    return assess(subsignals, missing_signals)


if __name__ == "__main__":
    result = compute_signal()
    print(f"Checklist: {result.label}  ({result.detail})")
    if result.missing_signals:
        print(f"Missing: {', '.join(result.missing_signals)}")
    for signal in result.subsignals:
        status = result.status(signal.name) if signal.name in REQUIRED_SIGNALS else "context"
        print(f"  - {signal.name:12s} {status:10s} {signal.state:14s} {signal.detail}")
