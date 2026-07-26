"""Score indicator #1: VIX level -> fear / buying-opportunity signal."""

from collectors.vix import get_latest_vix, load_latest_cached_vix, vix_change_pct
from config import VIX_OPTIMISTIC, VIX_SOFT, VIX_STRONG
from signals.base import NONE, SOFT, STRONG, SubSignal


def _change_suffix(change: float | None) -> str:
    return f", {change:+.1%} vs prior close" if change is not None else ""


def score(vix: float | None = None, allow_refresh: bool = True) -> SubSignal:
    """Map the latest VIX to a SubSignal.

    High VIX = fear = potential buying opportunity (esp. on a dip).

    allow_refresh=False serves the cached value without fetching (e.g. the market's closed,
    so nothing new to find) — falls back to a live fetch if there's no cache yet.
    """
    if vix is None:
        vix = get_latest_vix() if allow_refresh else (load_latest_cached_vix() or get_latest_vix())
    change = _change_suffix(vix_change_pct())

    if vix >= VIX_STRONG:
        return SubSignal("vix", vix, STRONG, f"VIX {vix:.1f} >= {VIX_STRONG}, high fear{change}", passes=True)
    if vix >= VIX_SOFT:
        return SubSignal("vix", vix, SOFT, f"VIX {vix:.1f} in soft band{change}", passes=True)
    if vix <= VIX_OPTIMISTIC:
        return SubSignal("vix", vix, NONE, f"VIX {vix:.1f} very low, optimistic/complacent{change}", passes=False)
    return SubSignal("vix", vix, NONE, f"VIX {vix:.1f} calm, no signal{change}", passes=False)
