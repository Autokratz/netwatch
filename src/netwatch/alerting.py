"""Threshold evaluation with hysteresis.

A monitor that alerts on the first breach and clears on the first good sample
produces flapping, and a flapping alert is worse than no alert: people learn to
ignore the channel, and then miss the real one. So a breach must persist for
``for_breaches`` consecutive evaluations before it fires, and recovery must
persist for ``clear_after`` before it resolves.

The asymmetry is deliberate - it should be harder to clear than to fire.
Defaults are 3 to fire and 5 to clear. See docs/adr/0003.

Events are emitted on TRANSITIONS only. A firing alert that stays firing
produces nothing, which is what makes the output readable during a long
incident.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from netwatch.window import WindowStats

__all__ = ["AlertEngine", "AlertEvent", "AlertState", "Thresholds"]


class AlertState(Enum):
    OK = "ok"
    FIRING = "firing"


@dataclass(frozen=True, slots=True)
class Thresholds:
    """Limits for one target. ``None`` disables that check."""

    rtt_p95_ms: float | None = None
    loss_pct: float | None = None
    for_breaches: int = 3
    clear_after: int = 5

    def __post_init__(self) -> None:
        if self.for_breaches < 1:
            raise ValueError("for_breaches must be at least 1")
        if self.clear_after < 1:
            raise ValueError("clear_after must be at least 1")
        if self.rtt_p95_ms is None and self.loss_pct is None:
            raise ValueError("a target needs at least one threshold to be worth watching")
        if self.rtt_p95_ms is not None and self.rtt_p95_ms <= 0:
            raise ValueError("rtt_p95_ms must be positive")
        if self.loss_pct is not None and not 0 <= self.loss_pct <= 100:
            raise ValueError("loss_pct must be between 0 and 100")


@dataclass(frozen=True, slots=True)
class AlertEvent:
    """A state transition. ``reasons`` explains why, in the operator's words."""

    target: str
    state: AlertState
    reasons: tuple[str, ...]
    stats: WindowStats

    def format_line(self) -> str:
        verb = "FIRING " if self.state is AlertState.FIRING else "RESOLVED"
        detail = "; ".join(self.reasons) if self.reasons else "all thresholds back within limits"
        return f"[{verb}] {self.target}: {detail}"


def evaluate(stats: WindowStats, thresholds: Thresholds) -> tuple[str, ...]:
    """Return one reason per breached threshold, empty when all are satisfied.

    An empty window returns no reasons. Treating "no data yet" as a breach
    fires an alert on every start-up, which trains people to dismiss it.
    """
    if stats.is_empty:
        return ()

    reasons: list[str] = []

    if thresholds.loss_pct is not None and stats.loss_pct > thresholds.loss_pct:
        reasons.append(
            f"loss {stats.loss_pct:.1f}% over {thresholds.loss_pct:.1f}% "
            f"({stats.failures}/{stats.samples} failed)"
        )

    if thresholds.rtt_p95_ms is not None:
        if stats.rtt_p95_ms is None:
            # Every sample failed, so there is no latency to compare. The loss
            # check owns this case; reporting "p95 unknown" as a latency breach
            # would double-count one fault as two.
            if thresholds.loss_pct is None:
                reasons.append("no successful samples in window")
        elif stats.rtt_p95_ms > thresholds.rtt_p95_ms:
            reasons.append(f"p95 {stats.rtt_p95_ms:.1f}ms over {thresholds.rtt_p95_ms:.1f}ms")

    return tuple(reasons)


@dataclass(slots=True)
class _TargetState:
    """One streak, because a target is either breaching or clear, never both."""

    state: AlertState = AlertState.OK
    breaching: bool = False
    streak: int = 0


class AlertEngine:
    """Tracks alert state per target and emits transitions.

    Holds no clock and does no I/O, so its behaviour is entirely determined by
    the sequence of stats fed to it - which is what makes the hysteresis
    testable without waiting for real time to pass.
    """

    __slots__ = ("_states", "_thresholds")

    def __init__(self, thresholds: dict[str, Thresholds]) -> None:
        self._thresholds = thresholds
        self._states: dict[str, _TargetState] = {name: _TargetState() for name in thresholds}

    def state_of(self, target: str) -> AlertState:
        return self._states[target].state

    def observe(self, stats: WindowStats) -> AlertEvent | None:
        """Feed one window summary in. Returns an event only on a transition.

        Raises:
            KeyError: if the target has no configured thresholds.
        """
        thresholds = self._thresholds[stats.target]
        target_state = self._states[stats.target]

        reasons = evaluate(stats, thresholds)
        breaching = bool(reasons)

        # Same verdict as last time extends the streak; a different one starts over.
        target_state.streak = target_state.streak + 1 if breaching == target_state.breaching else 1
        target_state.breaching = breaching

        wanted = AlertState.FIRING if breaching else AlertState.OK
        needed = thresholds.for_breaches if breaching else thresholds.clear_after
        if target_state.state is wanted or target_state.streak < needed:
            return None

        target_state.state = wanted
        return AlertEvent(stats.target, wanted, reasons, stats)
