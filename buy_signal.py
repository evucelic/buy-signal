"""Combine the sub-signals into one weighted-average buy signal."""

from dataclasses import dataclass

from config import SIGNAL_WEIGHTS
from signals import rate_signal, vix_signal
# from signals import margin_signal, sector_signal  # enable as built


@dataclass
class BuySignal:
    score: float            # weighted aggregate
    state: str              # "none" | "soft" | "strong"
    subsignals: list       # the underlying SubSignal objects


def compute_signal(vix: float | None = None) -> BuySignal:
    """Score every available sub-signal and combine them; unbuilt/failing ones are skipped."""
    scorers = [
        lambda: vix_signal.score(vix),
        rate_signal.score,
        # margin_signal.score,
        # sector_signal.score,
    ]
    subs = []
    for scorer in scorers:
        try:
            subs.append(scorer())
        except NotImplementedError:
            continue  # collector not built yet
        except Exception as exc:  # a flaky/missing source must not kill the signal
            print(f"signal skipped ({type(exc).__name__}: {exc})")

    if not subs:
        return BuySignal(0.0, "none", subs)

    total_weight = sum(SIGNAL_WEIGHTS.get(s.name, 1.0) for s in subs)
    score = sum(s.score * SIGNAL_WEIGHTS.get(s.name, 1.0) for s in subs) / total_weight

    if score >= 0.66:
        state = "strong"
    elif score >= 0.33:
        state = "soft"
    else:
        state = "none"

    return BuySignal(score, state, subs)


if __name__ == "__main__":
    result = compute_signal()
    print(f"Signal: {result.state.upper()}  (score {result.score:+.2f})")
    for s in result.subsignals:
        print(f"  - {s.name:12s} {s.state:6s} {s.detail}")
