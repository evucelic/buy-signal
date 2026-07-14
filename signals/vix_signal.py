"""Score indicator #1: VIX level -> fear / buying-opportunity signal."""

from collectors.vix import get_latest_vix
from config import VIX_OPTIMISTIC, VIX_SOFT, VIX_STRONG
from signals.base import NONE, SOFT, STRONG, SubSignal


def score(vix: float | None = None) -> SubSignal:
    """Map the latest VIX to a SubSignal.

    High VIX = fear = potential buying opportunity (esp. on a dip).
    """
    if vix is None:
        vix = get_latest_vix()

    if vix >= VIX_STRONG:
        return SubSignal("vix", vix, STRONG, f"VIX {vix:.1f} >= {VIX_STRONG}, high fear", passes=True)
    if vix >= VIX_SOFT:
        return SubSignal("vix", vix, SOFT, f"VIX {vix:.1f} in soft band", passes=True)
    if vix <= VIX_OPTIMISTIC:
        return SubSignal("vix", vix, NONE, f"VIX {vix:.1f} very low, optimistic/complacent", passes=False)
    return SubSignal("vix", vix, NONE, f"VIX {vix:.1f} calm, no signal", passes=False)
