"""Score indicators #4 & #5: clear leading sector + its earnings trajectory.

TODO: implement once collectors.sectors is done. Combines:
  #4 a clear leading sector exists (relative strength stands out), and
  #5 that sector's earnings profile is trending up.
"""

from signals.base import SubSignal


def score():
    raise NotImplementedError
    # Sketch:
    #   perf = sector_performance()
    #   leader = perf.idxmax(); clear = leader margin over runner-up is large
    #   earnings_up = leader's earnings trajectory rising
    #   return SubSignal("sector", ..., detail=f"{leader} leading, earnings up")
