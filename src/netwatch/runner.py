"""The monitoring loop.

Time and probes are injected rather than imported, so the whole loop can be
driven deterministically in a test: a fake clock advances instantly and fake
probes return a scripted sequence. A loop that can only be tested by waiting
for real seconds to pass does not get tested.

The loop never lets one target's failure affect another. A probe that somehow
raises despite the contract is caught, recorded as a failed sample, and the
tick continues - one bad target must not stop the monitor.
"""

from __future__ import annotations

import itertools
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field

from netwatch.alerting import AlertEngine, AlertEvent, AlertState
from netwatch.config import Config
from netwatch.probes import Probe, ProbeResult, build_probe
from netwatch.storage import HistoryWriter
from netwatch.window import RollingWindow, WindowStats

__all__ = ["Monitor", "TickOutcome"]


@dataclass(frozen=True, slots=True)
class TickOutcome:
    """Everything that happened in one pass over the targets."""

    results: tuple[ProbeResult, ...]
    stats: tuple[WindowStats, ...]
    events: tuple[AlertEvent, ...] = field(default=())


class Monitor:
    """Owns the windows, the alert engine and the optional history writer."""

    __slots__ = ("_engine", "_history", "_probes", "_sleep", "_windows")

    def __init__(
        self,
        config: Config,
        probes: Sequence[Probe] | None = None,
        history: HistoryWriter | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._probes: list[Probe] = list(
            probes
            if probes is not None
            else [build_probe(t.name, t.kind, t.host, t.port, t.timeout_s) for t in config.targets]
        )
        self._windows = {p.name: RollingWindow(p.name, config.window_size) for p in self._probes}
        self._engine = AlertEngine(config.thresholds_by_target())
        self._history = history
        self._sleep = sleep

    def tick(self) -> TickOutcome:
        """Probe every target once and evaluate the result."""
        results: list[ProbeResult] = []
        stats: list[WindowStats] = []
        events: list[AlertEvent] = []

        for probe in self._probes:
            try:
                result = probe.measure()
            except Exception as exc:  # noqa: BLE001 - a probe must never stop the loop
                result = ProbeResult(
                    probe.name, time.time(), False, error=f"probe raised: {exc}"[:120]
                )

            results.append(result)
            window = self._windows[result.target]
            window.add(result)

            if self._history is not None:
                try:
                    self._history.append(result)
                except OSError as exc:
                    # A full disk is not a network fault. Recording it as one
                    # would inflate loss and could fire a false alert, so the
                    # measurement stays truthful and the sink is dropped
                    # instead of failing again on every tick.
                    print(
                        f"netwatch: history write failed, continuing without it: {exc}",
                        file=sys.stderr,
                    )
                    self._history = None

            summary = window.stats()
            stats.append(summary)

            event = self._engine.observe(summary)
            if event is not None:
                events.append(event)

        return TickOutcome(tuple(results), tuple(stats), tuple(events))

    def run(
        self,
        interval_s: float,
        ticks: int | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> Iterator[TickOutcome]:
        """Yield one outcome per pass, forever when ``ticks`` is None.

        Yielding rather than accumulating lets the caller print each pass as it
        happens, which is why the CLI no longer needs its own copy of this loop.
        ``should_stop`` is consulted before the wait and again after it, so an
        interrupt arriving mid-wait does not buy one more full round of probes.

        The wait is the remainder of the interval, not the whole of it, so a
        slow probe does not make the schedule drift.
        """
        passes = itertools.count() if ticks is None else range(ticks)
        due: float | None = None

        for _ in passes:
            if should_stop is not None and should_stop():
                return
            if due is not None:
                self._sleep(max(0.0, due - time.perf_counter()))
                if should_stop is not None and should_stop():
                    return
            due = time.perf_counter() + interval_s
            yield self.tick()

    def current_stats(self) -> list[WindowStats]:
        return [w.stats() for w in self._windows.values()]

    def alert_states(self) -> dict[str, AlertState]:
        return {name: self._engine.state_of(name) for name in self._windows}
