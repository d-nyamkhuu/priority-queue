"""The percentile shared by the load generator, the analysis and the figures.

Pure Python on purpose: the load generator runs in a small image without
numpy, and every table and chart must use the same percentile definition.
"""

from __future__ import annotations

import math
from collections.abc import Iterable


def percentile(values: Iterable[float | None], q: float) -> float:
    """The q-th percentile (0-100) with linear interpolation between ranks.

    Same definition as numpy's default (``method="linear"``, Hyndman & Fan
    type 7). None and NaN are ignored; an empty input gives NaN.
    """
    xs = sorted(float(v) for v in values if v is not None and not math.isnan(v))
    if not xs:
        return float("nan")
    pos = (len(xs) - 1) * q / 100.0
    lo = math.floor(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)
