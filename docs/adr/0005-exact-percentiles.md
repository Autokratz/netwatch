# 5. Compute exact percentiles by sorting

**Status:** accepted · 2026-09

## Context

Percentiles over a stream are usually computed with an estimator — t-digest,
HDR histogram, reservoir sampling — because keeping every sample is impractical
at scale.

## Decision

Sort the window and take the nearest rank. The window is bounded by
`window_size`, typically 10 to 60 samples.

## Consequences

**At this size, sorting is cheaper than the estimator.** Sorting 60 floats is
faster than maintaining a t-digest, and it involves no dependency.

**The answer is exact and reproducible.** Two people computing p95 from the same
history file get the same number. An estimator gives an approximation whose
error depends on insertion order, which is a genuinely bad property when the
number is being read out during an incident and compared against a threshold.

**Nearest-rank, not interpolated.** The reported p95 is always a value that was
actually measured, not an average of two neighbours. "The 95th percentile was
118ms" should mean a real probe took 118ms.

**This does not scale**, and it does not need to. If the window ever needed to
hold a million samples the design would be wrong for other reasons first — this
is a single-process tool watching tens of targets, not a metrics backend.
