"""Score indicator #1: VIX level -> fear / buying-opportunity signal."""

import pandas as pd

from buysignal.collectors.vix import _vix_session_is_current, daily_history, get_latest_vix, load_latest_cached_vix, vix_change_pct
from buysignal.config import VIX_CSV, VIX_OPTIMISTIC, VIX_SOFT, VIX_STRONG
from buysignal.signals.base import NONE, SOFT, STRONG, SubSignal


def _change_suffix(change: float | None) -> str:
    return f", {change:+.1%} vs prior close" if change is not None else ""


def score(vix: float | None = None, allow_refresh: bool = True) -> SubSignal:
    """Map the latest VIX to a SubSignal.

    High VIX = fear = potential buying opportunity (esp. on a dip).

    allow_refresh=False serves the cached value without fetching (e.g. the market's closed,
    so nothing new to find); falls back to a live fetch if there's no cache yet.
    """
    if vix is None:
        if allow_refresh:
            vix = get_latest_vix()
        else:
            vix = load_latest_cached_vix()
            if vix is None:
                vix = get_latest_vix()
            elif not _vix_session_is_current(VIX_CSV, pd.Timestamp.now(tz="UTC")):
                raise ValueError("VIX observation predates the expected session")
    change = _change_suffix(vix_change_pct())

    if vix >= VIX_STRONG:
        state, detail, passes = STRONG, f"VIX {vix:.1f} >= {VIX_STRONG}, high fear{change}", True
    elif vix >= VIX_SOFT:
        state, detail, passes = SOFT, f"VIX {vix:.1f} in soft band{change}", True
    elif vix <= VIX_OPTIMISTIC:
        state, detail, passes = NONE, f"VIX {vix:.1f} very low, optimistic/complacent{change}", False
    else:
        state, detail, passes = NONE, f"VIX {vix:.1f} calm, no signal{change}", False
    history, observed = None, None
    metrics = {"vix": vix, "soft_threshold": VIX_SOFT, "strong_threshold": VIX_STRONG}
    try:
        cached = daily_history()
        if not cached.empty and abs(float(cached.vix.iloc[-1]) - vix) < 0.01:
            history = cached.tail(252).copy()
            observed = str(cached.date.iloc[-1].date())
            peak = float(cached.vix.tail(20).max())
            metrics["recent_peak"] = peak
            detail += f" | Recent 20-session VIX high {peak:.1f}; latest is {vix / peak - 1:+.1%} from that high"
    except (OSError, ValueError, KeyError):
        pass
    return SubSignal(
        "vix", vix, state, detail, passes=passes, observed_at=observed,
        metrics=metrics, history=history,
        footer=f"Rule ≥{VIX_SOFT:g} · High ≥{VIX_STRONG:g}",
    )
