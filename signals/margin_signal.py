"""Score indicator #3: FINRA margin debt decreasing (deleveraging).

TODO: implement once collectors.margin_debt is done.
At least MARGIN_DELEVERAGE_MONTHS of decrease confirms deleveraging -> positive.
"""

from signals.base import SubSignal


def score():
    raise NotImplementedError
    # Sketch:
    #   hist = margin_history()
    #   decreasing = all(hist diffs over last N months are negative)
    #   return SubSignal("margin_debt", 1.0 if decreasing else -0.5, ...)
