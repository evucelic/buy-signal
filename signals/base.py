from dataclasses import dataclass

NONE = "none"
SOFT = "soft"
STRONG = "strong"


@dataclass
class SubSignal:
    name: str       # "vix", "fed_rate", "margin_debt", "sector"
    score: float    # signal-specific numeric diagnostic
    state: str      # free-form label, e.g. none/soft/strong (vix) or cutting/flat/hiking (fed)
    detail: str     # human-readable reason
    passes: bool = False
