# netwatch

**Threshold monitoring for network targets, with hysteresis, CSV history and a non-zero exit code you can gate a change on.**

[![CI](https://github.com/Autokratz/netwatch/actions/workflows/ci.yml/badge.svg)](https://github.com/Autokratz/netwatch/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)
![License](https://img.shields.io/badge/license-MIT-green)

```console
$ netwatch check -c examples/netwatch.toml -n 10
TARGET     SAMPLES  LOSS%  AVG ms  P95 ms  MAX ms   STATE
---------  -------  -----  ------  ------  ------  ------
api             10    0.0    18.4    22.1    22.9      OK
gateway         10    0.0     0.4     0.6     0.6      OK
internet        10   10.0    14.2   118.0   118.0  FIRING

FIRING: internet
$ echo $?
1
```

---

## Why it exists

Monitoring tools either want a server, a database and a dashboard, or they are a
`ping` in a `while` loop that nobody can act on. This sits in between: one
dependency-free Python package that runs on a laptop or a jump box, keeps a
history you can open in a spreadsheet, and exits non-zero when something is
wrong so it can be used as a gate rather than only as a display.

It generalises the per-second counter sampling and before/after reporting I
wrote for the [network QoS lab](https://github.com/Autokratz/network-qos-lab),
where the measurement code was inline in the lab scripts. Here it is a tool.

---

## The part that matters: hysteresis

A monitor that alerts on the first breach and clears on the first good sample
flaps. A flapping alert is **worse than no alert**, because people learn to
ignore the channel and then miss the real one.

So a breach must persist for `for_breaches` consecutive evaluations before it
fires, and recovery must persist for `clear_after` before it resolves. The
asymmetry is deliberate — it should be harder to clear than to fire. Defaults
are 3 and 5.

```
samples   B B G B B G B B G B B G ...      alternating, a genuinely flapping link
netwatch  (silent — never reaches 3 in a row)

samples   G G B B B B B B G G G G G        a real fault, then recovery
netwatch        ^FIRING           ^RESOLVED
                (3rd breach)      (5th clear)
```

Events are emitted on **transitions only**. A firing alert that stays firing
produces nothing, which is what keeps the output readable during a long
incident.

`tests/test_alerting.py` covers this specifically, including
`test_flapping_input_produces_no_events_at_all`.

---

## Install

```bash
pip install -e ".[dev]"     # from a clone
netwatch --version
```

Python 3.11 or newer. **No runtime dependencies** — the `dev` extra pulls in
pytest, mypy and ruff, and nothing else is needed to run it.

---

## Use

```bash
netwatch run   -c netwatch.toml          # continuous, Ctrl-C to stop
netwatch run   -c netwatch.toml -q       # alerts only, no table
netwatch check -c netwatch.toml -n 10    # 10 passes, exit 1 if anything is firing
netwatch report out/history.csv          # summarise a history file
```

### Exit codes

| Code | Meaning |
|---|---|
| `0` | everything within thresholds |
| `1` | at least one target was firing when the run ended |
| `2` | the configuration could not be loaded |

`check` is the reason those exist. It makes netwatch usable as a post-change
gate — run it after a firewall change or a link cutover, and let a non-zero
exit fail the pipeline.

### Configuration

```toml
interval_s   = 10.0
window_size  = 12              # 12 samples x 10s = a 2 minute window
history_path = "out/history.csv"

[[target]]
name       = "gateway"
kind       = "icmp"
host       = "10.20.0.1"
rtt_p95_ms = 20.0
loss_pct   = 5.0

[[target]]
name       = "api"
kind       = "tcp"             # a handshake proves the service accepts
host       = "api.internal"    # connections; an echo reply does not
port       = 443
rtt_p95_ms = 250.0
```

Validation is strict and happens once, at load. A monitor that starts with a
typo in a threshold and discovers it three hours later — when the alert that
should have fired did not — is worse than one that refuses to start. Every
error message names the target it came from.

Full reference: [docs/CONFIGURATION.md](docs/CONFIGURATION.md).

---

## Design decisions

Each of these is recorded as an ADR in [docs/adr/](docs/adr/), with the
alternatives that were rejected and why.

| Decision | Short reason |
|---|---|
| [ICMP by shelling out to `ping`](docs/adr/0002-icmp-via-subprocess.md) | Raw sockets need `CAP_NET_RAW` or root. A monitor that must run as root is a worse monitor than one that parses text. |
| [Hysteresis, asymmetric](docs/adr/0003-hysteresis-for-alerting.md) | Flapping alerts get ignored, and an ignored channel is an outage you find out about from a user. |
| [CSV, not a database](docs/adr/0004-csv-history.md) | The history has to be readable by whoever inherits this, on a jump box, with no tooling. |
| [Exact percentiles, not streaming](docs/adr/0005-exact-percentiles.md) | At tens of samples, sorting is cheaper than an estimator and gives the same answer every time. |

---

## Architecture

```
cli.py        argparse, exit codes, signal handling
 └── runner.py      the loop; time and probes injected, so it is testable
      ├── probes.py     IcmpProbe / TcpProbe behind a Protocol. Never raise.
      ├── window.py     fixed-size deque + statistics over it
      ├── alerting.py   threshold evaluation with hysteresis. No I/O, no clock.
      └── storage.py    append-only CSV, flushed per row
report.py     fixed-width table rendering
config.py     strict TOML loading
```

Two properties make the whole thing testable, and both were deliberate:

**Probes never raise.** An unreachable host and a malformed hostname are both
just data — a `ProbeResult` with `success=False` and a readable `error`. The
runner catches anything that violates that contract anyway and records it as a
failed sample, because one bad target must not stop the monitor.

**The alert engine holds no clock and does no I/O.** Its behaviour is entirely
determined by the sequence of window summaries fed to it, which is why twenty
minutes of hysteresis can be exercised in a millisecond.

---

## Development

```bash
pytest                       # the suite
pytest --cov=netwatch        # with coverage
ruff check . && ruff format --check .
mypy                         # strict mode, src and tests
```

CI runs all four on Python 3.11, 3.12 and 3.13.

---

## What this deliberately does not do

**No notification backends.** No email, Slack or PagerDuty. Events go to stdout
with a stable format and a meaningful exit code; piping that into whatever your
organisation already uses is a two-line shell script, and building four
half-supported integrations is how a small tool becomes an unmaintained one.

**No daemon, no service file.** `cron`, `systemd` and a terminal window all
already know how to run a program on a schedule.

**No config hot-reload.** Restarting is fast and unambiguous. Reload introduces
a state machine where a target can change thresholds mid-incident, which is
hard to reason about at 3am for a feature nobody asked for.

---

Built by [Hector Cabra](https://autokratz.github.io) · MIT
