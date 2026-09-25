"""Render window statistics as a fixed-width table.

Plain text, no dependencies, and aligned on the decimal point. This output gets
pasted into tickets and read over a phone, so the columns have to line up in a
terminal that has no colour and no unicode.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence

from netwatch.alerting import AlertState
from netwatch.probes import ProbeResult
from netwatch.window import WindowStats

__all__ = ["render_table", "summarise_history"]

_HEADERS = ("TARGET", "SAMPLES", "LOSS%", "AVG ms", "P95 ms", "MAX ms", "STATE")


def _fmt(value: float | None, places: int = 1) -> str:
    return "-" if value is None else f"{value:.{places}f}"


def render_table(
    rows: Sequence[WindowStats],
    states: dict[str, AlertState] | None = None,
) -> str:
    """Render one row per target. Returns a message rather than an empty table
    when there is nothing to show, because a bare header reads as a bug."""
    if not rows:
        return "no data"

    states = states or {}

    body = [
        (
            r.target,
            str(r.samples),
            _fmt(r.loss_pct),
            _fmt(r.rtt_avg_ms),
            _fmt(r.rtt_p95_ms),
            _fmt(r.rtt_max_ms),
            states.get(r.target, AlertState.OK).value.upper(),
        )
        for r in rows
    ]

    widths = [max(len(_HEADERS[i]), max(len(row[i]) for row in body)) for i in range(len(_HEADERS))]

    def line(cells: Sequence[str]) -> str:
        # First column left-aligned (names vary in length), the rest right so
        # the digits stack.
        parts = [cells[0].ljust(widths[0])]
        parts += [cells[i].rjust(widths[i]) for i in range(1, len(cells))]
        return "  ".join(parts).rstrip()

    out = [line(_HEADERS), "  ".join("-" * w for w in widths)]
    out.extend(line(row) for row in body)
    return "\n".join(out)


def summarise_history(results: Iterable[ProbeResult]) -> list[WindowStats]:
    """Group history rows by target and summarise each.

    Unbounded by design: a history file is finite and already on disk, so the
    rolling-window cap that protects the live loop is not needed here.
    """
    buckets: defaultdict[str, list[ProbeResult]] = defaultdict(list)
    for result in results:
        buckets[result.target].append(result)
    return [WindowStats.from_results(t, rs) for t, rs in sorted(buckets.items())]
