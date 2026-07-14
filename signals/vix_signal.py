"""Score indicator #1: VIX level -> fear / buying-opportunity signal."""

from collectors.vix import latest_vix
from config import VIX_OPTIMISTIC, VIX_SOFT, VIX_STRONG
from signals.base import NONE, SOFT, STRONG, SubSignal


def score(vix=None):
    """Map the latest VIX to a SubSignal.

    High VIX = fear = potential buying opportunity (esp. on a dip).
    """
    if vix is None:
        vix = latest_vix()

    if vix >= VIX_STRONG:
        return SubSignal("vix", 1.0, STRONG, f"VIX {vix:.1f} >= {VIX_STRONG}, high fear")
    if vix >= VIX_SOFT:
        return SubSignal("vix", 0.5, SOFT, f"VIX {vix:.1f} in soft band")
    if vix <= VIX_OPTIMISTIC:
        return SubSignal("vix", -0.5, NONE, f"VIX {vix:.1f} very low, optimistic/complacent")
    return SubSignal("vix", 0.0, NONE, f"VIX {vix:.1f} calm, no signal")
