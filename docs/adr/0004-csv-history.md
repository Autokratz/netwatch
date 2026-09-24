# 4. Store history as append-only CSV

**Status:** accepted · 2026-09

## Context

The history needs to survive restarts, be queryable after an incident, and be
readable by whoever inherits this — possibly on a jump box, at 3am, with no
tooling and no internet.

## Decision

Append-only CSV, one row per sample, flushed after every write.

## Consequences

**Everything opens a CSV.** `cut`, `awk`, `grep`, Excel, pandas, a text editor.
A SQLite file needs a client; a custom binary format needs this tool, which is
precisely the thing that might not be installed on the machine you are on.

**Flushed per row**, not buffered. The history exists for the outage that
killed the process; buffering loses exactly the rows that matter. The cost is a
`write` syscall per sample per target, which at ten targets on a ten-second
interval is one write per second.

**The header is written only on creation**, so restarting appends rather than
corrupting the file with a second header partway down.

**Damaged rows are skipped, not fatal.** An append-only file can be truncated
mid-write by the very outage being investigated. One unparseable final row must
not make the preceding thousand unreadable.

**What this gives up:** indexed queries, retention policies, and concurrent
writers. Retention is `logrotate`'s job. Concurrent writers are out of scope —
one process owns one history file.

**Rejected:** SQLite. Better queries, but it introduces a file format that
needs a client, and the queries this tool needs are "show me the last hour",
which `tail` answers.
