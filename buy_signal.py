"""Combine the sub-signals into a checklist-style alert."""

from dataclasses import dataclass, field
from typing import Callable

from signals import rate_signal, vix_signal
from signals.base import SubSignal
# from signals import margin_signal, sector_signal  # enable as built


@dataclass
class BuySignal:
    score: float
    state: str
    passing_count: int
    subsignals: list[SubSignal]
    missing_signals: list[str] = field(default_factory=list)
    explanation: str = ""


Scorer = tuple[str, Callable[[], SubSignal]]


def _evaluate_alert(subsignals_by_name: dict[str, SubSignal], missing_signals: list[str]) -> tuple[str, str]:
    vix = subsignals_by_name.get("vix")
    fed_rate = subsignals_by_name.get("fed_rate")

    if vix is None or fed_rate is None:
        return "none", "NONE: VIX or rate signal is unavailable."
    if not vix.passes or not fed_rate.passes:
        return "none", "NONE: VIX fear and non-hiking rates are not both present."

    if missing_signals:
        return (
            "soft",
            "SOFT: VIX fear + non-hiking rates; missing signals: " + ", ".join(sorted(set(missing_signals))),
        )
    return "strong", "STRONG: VIX fear + non-hiking rates."


def compute_signal(vix: float | None = None) -> BuySignal:
    """Score every available sub-signal and combine them via explicit checklist rules."""
    scorers: list[Scorer] = [
        ("vix", lambda: vix_signal.score(vix)),
        ("fed_rate", rate_signal.score),
        # ("margin_debt", margin_signal.score),
        # ("sector", sector_signal.score),
    ]

    subsignals_by_name: dict[str, SubSignal] = {}
    missing_signals: list[str] = []

    for name, scorer in scorers:
        try:
            signal = scorer()
        except NotImplementedError:
            missing_signals.append(name)
            continue
        except Exception as exc:
            missing_signals.append(name)
            print(f"signal skipped ({name}: {type(exc).__name__}: {exc})")
            continue

        subsignals_by_name[name] = signal
        if not signal.passes and signal.state == "unavailable":
            missing_signals.append(name)

    subsignals = list(subsignals_by_name.values())
    passing_count = sum(1 for signal in subsignals if signal.passes)
    score = passing_count / len(subsignals) if subsignals else 0.0
    state, explanation = _evaluate_alert(subsignals_by_name, missing_signals)

    return BuySignal(
        score=score,
        state=state,
        passing_count=passing_count,
        subsignals=subsignals,
        missing_signals=sorted(set(missing_signals)),
        explanation=explanation,
    )


if __name__ == "__main__":
    result = compute_signal()
    print(f"Alert: {result.state.upper()}  (passing {result.passing_count}/{len(result.subsignals)}; score {result.score:+.2f})")
    print(f"Rule: {result.explanation}")
    if result.missing_signals:
        print(f"Missing: {', '.join(result.missing_signals)}")
    for signal in result.subsignals:
        status = "PASS" if signal.passes else "FAIL"
        print(f"  - {signal.name:12s} {status:4s} {signal.state:11s} {signal.detail}")
