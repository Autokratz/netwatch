"""Shared fixtures.

Nothing in the unit suite touches the network or the clock. Probes are scripted
and time is supplied, so a run that exercises twenty minutes of hysteresis
finishes in milliseconds and gives the same answer every time.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field

import pytest

from netwatch.alerting import Thresholds
from netwatch.config import Config, TargetConfig
from netwatch.probes import ProbeResult


@dataclass
class FakeProbe:
    """Returns a scripted sequence of results, then repeats the last one."""

    name: str
    script: Sequence[ProbeResult]
    calls: int = field(default=0)

    def measure(self) -> ProbeResult:
        index = min(self.calls, len(self.script) - 1)
        self.calls += 1
        return self.script[index]


@dataclass
class RaisingProbe:
    """Violates the contract on purpose, to prove the loop survives it."""

    name: str
    calls: int = 0

    def measure(self) -> ProbeResult:
        self.calls += 1
        raise RuntimeError("probe exploded")


def ok(target: str, rtt: float, ts: float = 1000.0) -> ProbeResult:
    return ProbeResult(target=target, timestamp=ts, success=True, rtt_ms=rtt)


def fail(target: str, error: str = "timed out", ts: float = 1000.0) -> ProbeResult:
    return ProbeResult(target=target, timestamp=ts, success=False, error=error)


@pytest.fixture
def thresholds() -> Thresholds:
    return Thresholds(rtt_p95_ms=100.0, loss_pct=10.0, for_breaches=3, clear_after=5)


@pytest.fixture
def config(thresholds: Thresholds) -> Config:
    return Config(
        interval_s=1.0,
        window_size=5,
        targets=(
            TargetConfig(
                name="gateway",
                kind="icmp",
                host="10.0.0.1",
                thresholds=thresholds,
            ),
        ),
    )


@pytest.fixture
def no_sleep() -> Iterator[list[float]]:
    """Collects the durations the runner would have slept for."""
    recorded: list[float] = []
    yield recorded
