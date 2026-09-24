"""Probes measure one target, once, and never raise.

A probe that raises takes the monitoring loop down with it, which is the one
failure mode a monitor must not have. Every probe here converts its failures
into a :class:`ProbeResult` with ``success=False`` and a human-readable
``error``, so an unreachable host and a malformed hostname are both just data.

ICMP is measured by shelling out to the system ``ping`` rather than opening a
raw socket. See docs/adr/0002 for why.
"""

from __future__ import annotations

import re
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from typing import Protocol

__all__ = ["IcmpProbe", "Probe", "ProbeResult", "TcpProbe", "build_probe"]


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """One measurement. Immutable, so a window can hold it without copying."""

    target: str
    timestamp: float
    success: bool
    rtt_ms: float | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if self.success and self.rtt_ms is None:
            raise ValueError("a successful probe must carry an rtt_ms")
        if not self.success and self.rtt_ms is not None:
            raise ValueError("a failed probe must not carry an rtt_ms")


class Probe(Protocol):
    """Anything that can measure a target once.

    Tests substitute a deterministic fake rather than touching the network,
    which is the whole reason this is a Protocol and not a base class.
    """

    name: str

    def measure(self) -> ProbeResult:
        """Measure once. Must not raise."""
        ...


# ``ping`` output differs between iputils, BSD and busybox, but every variant
# renders the round-trip as "time=12.3 ms" or "time=12.3ms".
_RTT_PATTERN = re.compile(r"time[=<]\s*(?P<ms>\d+(?:\.\d+)?)\s*ms", re.IGNORECASE)


def parse_ping_rtt(output: str) -> float | None:
    """Extract the round-trip time in milliseconds from ``ping`` output.

    Returns ``None`` when no timing line is present, which is what a timeout
    or an unreachable host produces.
    """
    match = _RTT_PATTERN.search(output)
    if match is None:
        return None
    return float(match.group("ms"))


@dataclass(slots=True)
class IcmpProbe:
    """Measure round-trip time with a single ICMP echo via the system ``ping``."""

    name: str
    host: str
    timeout_s: float = 2.0

    def measure(self) -> ProbeResult:
        now = time.time()

        binary = shutil.which("ping")
        if binary is None:
            return ProbeResult(self.name, now, False, error="ping not found on PATH")

        # -n: never resolve addresses back to names, which would add DNS
        # latency to a measurement that is supposed to be about the path.
        argv = [binary, "-c", "1", "-n", "-W", str(int(max(1, self.timeout_s))), self.host]

        try:
            completed = subprocess.run(  # noqa: S603 - argv is built here, never shell
                argv,
                capture_output=True,
                text=True,
                timeout=self.timeout_s + 1.0,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ProbeResult(self.name, now, False, error="timed out")
        except OSError as exc:  # pragma: no cover - platform specific
            return ProbeResult(self.name, now, False, error=f"could not run ping: {exc}")

        if completed.returncode != 0:
            detail = (completed.stdout or completed.stderr or "").strip().splitlines()
            reason = detail[-1] if detail else f"ping exited {completed.returncode}"
            return ProbeResult(self.name, now, False, error=reason[:120])

        rtt = parse_ping_rtt(completed.stdout)
        if rtt is None:
            # Exit code 0 with no timing line should not happen, but trusting
            # an exit code over the actual output is how phantom data gets in.
            return ProbeResult(self.name, now, False, error="no timing in ping output")

        return ProbeResult(self.name, now, True, rtt_ms=rtt)


@dataclass(slots=True)
class TcpProbe:
    """Measure the time to complete a TCP handshake against host:port.

    Useful where ICMP is filtered, and a closer proxy for "can the application
    actually connect" than an echo reply is.
    """

    name: str
    host: str
    port: int
    timeout_s: float = 2.0

    def measure(self) -> ProbeResult:
        now = time.time()
        started = time.perf_counter()
        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout_s):
                elapsed_ms = (time.perf_counter() - started) * 1000.0
        except TimeoutError:
            return ProbeResult(self.name, now, False, error="timed out")
        except OSError as exc:
            return ProbeResult(self.name, now, False, error=str(exc)[:120])
        return ProbeResult(self.name, now, True, rtt_ms=elapsed_ms)


def build_probe(
    name: str,
    kind: str,
    host: str,
    port: int | None = None,
    timeout_s: float = 2.0,
) -> Probe:
    """Construct the probe named by ``kind``.

    Raises:
        ValueError: if the kind is unknown, or a TCP probe is missing its port.
    """
    match kind:
        case "icmp":
            return IcmpProbe(name=name, host=host, timeout_s=timeout_s)
        case "tcp":
            if port is None:
                raise ValueError(f"target {name!r}: a tcp probe needs a port")
            return TcpProbe(name=name, host=host, port=port, timeout_s=timeout_s)
        case _:
            raise ValueError(f"target {name!r}: unknown probe kind {kind!r}")
