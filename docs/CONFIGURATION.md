# Configuration reference

TOML. One file, validated strictly at load.

> **TOML scoping.** Root keys must appear **before** the first `[[target]]`
> table. A key written after one belongs to that target, not to the root. This
> is the most common mistake with this format, so netwatch rejects unknown keys
> rather than silently ignoring a misplaced one.

## Root

| Key | Type | Default | Meaning |
|---|---|---|---|
| `interval_s` | float | `10.0` | Seconds between passes over all targets. Must be positive. |
| `window_size` | int | `10` | Samples retained per target. The window length in time is `window_size × interval_s`. |
| `history_path` | string | none | Where to append the CSV history. Omit to keep nothing. Parent directories are created. |

## `[[target]]`

One table per target. At least one is required, and names must be unique.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `name` | string | required | Identifier, used in output and in the history. |
| `kind` | `"icmp"` \| `"tcp"` | required | Probe type. |
| `host` | string | required | Hostname or address. |
| `port` | int | required for `tcp` | 1–65535. Must be **absent** for `icmp`. |
| `timeout_s` | float | `2.0` | Per-probe timeout. |
| `rtt_p95_ms` | float | none | Fire when the window's p95 exceeds this. |
| `loss_pct` | float | none | Fire when window loss exceeds this. `0.0` means any loss at all. |
| `for_breaches` | int | `3` | Consecutive breaching evaluations before firing. |
| `clear_after` | int | `5` | Consecutive clean evaluations before resolving. |

At least one of `rtt_p95_ms` or `loss_pct` is required — a target with no
threshold cannot alert, so configuring one is almost certainly a mistake.

## Choosing thresholds

**Comparison is strictly greater than.** `loss_pct = 10.0` tolerates exactly
10% and fires at 10.1%.

**Window length drives sensitivity.** With `window_size = 10`, one lost packet
is 10% loss. If you want to alert on a single loss, a long window will never
get there — shorten the window or lower the threshold.

**Set `loss_pct` from the window, not from a feeling.** One packet in a
12-sample window is 8.3%. A threshold of 5% therefore fires on every single
lost packet, which is usually not what was intended.

**Total loss is reported once.** When every sample fails there is no latency to
compare, so the loss threshold owns that case and the latency check stays
quiet. One fault produces one reason, not two.

## Worked example

```toml
interval_s   = 10.0
window_size  = 12              # a 2 minute window
history_path = "out/history.csv"

# The default gateway. Loss here means the local network, so the threshold is
# tight and the alert is meant to be believed.
[[target]]
name       = "gateway"
kind       = "icmp"
host       = "10.20.0.1"
rtt_p95_ms = 20.0
loss_pct   = 5.0

# A reference path to the internet. Far more variable, so a looser threshold
# and a longer confirmation before it fires.
[[target]]
name         = "internet"
kind         = "icmp"
host         = "1.1.1.1"
rtt_p95_ms   = 120.0
loss_pct     = 10.0
for_breaches = 5

# An application endpoint. TCP, because a completed handshake proves the
# service is accepting connections and an echo reply does not.
[[target]]
name       = "api"
kind       = "tcp"
host       = "api.internal"
port       = 443
timeout_s  = 3.0
rtt_p95_ms = 250.0
```

## Validation

Every error names the target it came from and, for an unknown key, lists the
valid ones:

```console
$ netwatch check -c broken.toml
netwatch: target 'gateway': unknown key 'los_pct'. Valid keys: clear_after,
for_breaches, host, kind, loss_pct, name, port, rtt_p95_ms, timeout_s
$ echo $?
2
```

The tool refuses to start rather than run with a threshold that was never
applied. A monitor that silently drops a typo'd key looks healthy and never
fires the alert it was configured for, which is the worst possible outcome.
