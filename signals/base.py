from dataclasses import dataclass

NONE = "none"
SOFT = "soft"
STRONG = "strong"


@dataclass
class SubSignal:
    name: str       # "vix", "fed_rate", "margin_debt", "sector"
    score: float    # -1.0 (bad) .. +1.0 (good to buy)
    state: str      # free-form label, e.g. none/soft/strong (vix) or cutting/flat/hiking (fed)
    detail: str     # human-readable reason
