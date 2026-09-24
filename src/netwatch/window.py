"""A fixed-size rolling window of probe results, and the statistics over it.

Thresholds are evaluated against a window rather than a single sample, because
one lost packet is weather and five in a row is a fault. The window is the unit
that makes that distinction possible.

Percentiles use the nearest-rank method on the sorted sample. For the window
sizes this tool uses - tens of samples, not millions - an exact sort is both
cheaper and easier to reason about than a streaming estimator, and it gives the
same answer every time, which matters when someone is reading the number off a
report during an incident.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

from netwatch.probes import ProbeResult

__all__ = ["RollingWindow", "WindowStats", "percentile"]


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile of a non-empty list.

    Args:
        values: sample values, in any order.
        pct: the percentile to take, 0 < pct <= 100.

    Raises:
        ValueError: if ``values`` is empty or ``pct`` is out of range.
    """
    if not values:
        raise ValueError("percentile of an empty sample is undefined")
    if not 0 < pct <= 100:
        raise ValueError(f"percentile must be in (0, 100], got {pct}")

    ordered = sorted(values)
    rank = math.ceil(pct / 100.0 * len(ordered))
    return ordered[rank - 1]


@dataclass(frozen=True, slots=True)
class WindowStats:
    """Summary of one window. ``rtt_*`` are ``None`` when nothing succeeded."""

    target: str
    samples: int
    successes: int
    failures: int
    loss_pct: float
    rtt_avg_ms: float | None
    rtt_p95_ms: float | None
    rtt_max_ms: float | None
    last_error: str | None

    @property
    def is_empty(self) -> bool:
        return self.samples == 0


class RollingWindow:
    """The last ``size`` results for one target.

    A deque with ``maxlen`` does the eviction, so appending is O(1) and the
    window can never grow without bound - which is the failure mode of every
    monitor that keeps "just the recent history" in a plain list.
    """

    __slots__ = ("_capacity", "_results", "_target")

    def __init__(self, target: str, size: int) -> None:
        if size < 1:
            raise ValueError(f"window size must be at least 1, got {size}")
        self._target = target
        self._capacity = size
        self._results: deque[ProbeResult] = deque(maxlen=size)

    @property
    def target(self) -> str:
        return self._target

    @property
    def capacity(self) -> int:
        return self._capacity

    def __len__(self) -> int:
        return len(self._results)

    def add(self, result: ProbeResult) -> None:
        """Append one result, evicting the oldest once the window is full."""
        if result.target != self._target:
            raise ValueError(
                f"result for {result.target!r} does not belong in the window for {self._target!r}"
            )
        self._results.append(result)

    def clear(self) -> None:
        self._results.clear()

    def stats(self) -> WindowStats:
        """Summarise the current contents. Cheap enough to call every tick."""
        total = len(self._results)
        if total == 0:
            return WindowStats(self._target, 0, 0, 0, 0.0, None, None, None, None)

        rtts = [r.rtt_ms for r in self._results if r.success and r.rtt_ms is not None]
        successes = len(rtts)
        failures = total - successes

        last_error = next(
            (r.error for r in reversed(self._results) if not r.success and r.error),
            None,
        )

        return WindowStats(
            target=self._target,
            samples=total,
            successes=successes,
            failures=failures,
            loss_pct=failures / total * 100.0,
            rtt_avg_ms=(sum(rtts) / successes) if rtts else None,
            rtt_p95_ms=percentile(rtts, 95.0) if rtts else None,
            rtt_max_ms=max(rtts) if rtts else None,
            last_error=last_error,
        )
