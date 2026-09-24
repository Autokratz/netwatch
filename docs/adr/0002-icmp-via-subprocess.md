# 2. Measure ICMP by shelling out to `ping`, not with raw sockets

**Status:** accepted · 2026-09

## Context

ICMP echo needs a raw socket. On Linux that requires `CAP_NET_RAW`, which in
practice means running as root, granting the capability on the interpreter, or
relying on `net.ipv4.ping_group_range` being set favourably.

## Decision

Shell out to the system `ping`, parse the round-trip time out of its output.

## Consequences

**What this buys.** The tool runs as an ordinary user, on any machine, with no
setup and no capability grant. A monitoring tool that must run as root is a
worse monitoring tool — it will not be installed on the box where it is needed,
and if it is, it is a larger thing to trust.

**What it costs.** Parsing text, and `ping` output varies. `parse_ping_rtt`
handles iputils, BSD and busybox, plus the `time<1 ms` form, and every dialect
is covered by a test with real captured output. A new platform means a new
sample in `tests/test_probes.py`, which is a cheap and visible failure mode.

**Rejected:** a raw-socket implementation gated behind a capability check with
the subprocess as fallback. That is two code paths, one of which is almost
never exercised, for a measurement difference of well under a millisecond.

**Also relevant:** `-n` is passed so `ping` never resolves addresses back to
names. Without it, reverse DNS latency lands inside a measurement that is
supposed to be about the network path.
