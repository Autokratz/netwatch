# 3. Alert with asymmetric hysteresis

**Status:** accepted · 2026-09

## Context

The naive implementation fires on the first breached sample and clears on the
first good one. Against a genuinely flapping link that produces an alert and a
recovery every interval.

A channel that cries wolf gets muted, and a muted channel is an outage you find
out about from a user instead of from your monitoring. This is the failure mode
that actually matters — not a missed sample, but a system nobody trusts.

## Decision

A breach must persist for `for_breaches` consecutive evaluations before firing.
Recovery must persist for `clear_after` consecutive evaluations before
resolving. Defaults 3 and 5 — deliberately asymmetric, because it should be
harder to convince the system a fault is over than that it started.

Events are emitted on transitions only. A firing alert that remains firing
produces no further output.

## Consequences

**Detection is slower**, by `for_breaches × interval`. At the default 10s
interval that is 30 seconds. For a tool whose window is already measured in
minutes, that is the right trade.

**Both counts are per-target and configurable.** A link where a single lost
packet genuinely matters can set `for_breaches = 1`.

**It is testable without a clock**, because the alert engine holds no time and
does no I/O. `test_flapping_input_produces_no_events_at_all` feeds forty
alternating samples and asserts total silence.
