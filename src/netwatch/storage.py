"""Append probe results to a CSV history, and read them back.

CSV rather than a database, for the reason in docs/adr/0004: the history has to
be readable by whoever inherits this, on a jump box, with no tooling. `cut`,
`awk` and a spreadsheet all open a CSV. Nothing opens a proprietary file.

The file handle is held open for the life of the writer and flushed after each
append. Reopening per row is slow; buffering without flushing loses the last
minutes of history in exactly the situation the history is for - the machine
went down.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

from netwatch.probes import ProbeResult

__all__ = ["FIELDNAMES", "HistoryWriter", "read_history"]

FIELDNAMES = ("timestamp", "iso_time", "target", "success", "rtt_ms", "error")


class HistoryWriter:
    """Append-only CSV writer. Use as a context manager.

    Writes the header only when creating a new file, so restarting the monitor
    against an existing history appends instead of corrupting it with a second
    header row halfway down.
    """

    __slots__ = ("_fh", "_path", "_writer")

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not path.exists() or path.stat().st_size == 0

        self._fh = path.open("a", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._fh, fieldnames=FIELDNAMES)
        if is_new:
            self._writer.writeheader()
            self._fh.flush()

    @property
    def path(self) -> Path:
        return self._path

    def append(self, result: ProbeResult) -> None:
        self._writer.writerow(
            {
                "timestamp": f"{result.timestamp:.3f}",
                "iso_time": datetime.fromtimestamp(result.timestamp, tz=UTC).isoformat(),
                "target": result.target,
                "success": "1" if result.success else "0",
                "rtt_ms": f"{result.rtt_ms:.3f}" if result.rtt_ms is not None else "",
                "error": result.error or "",
            }
        )
        self._fh.flush()

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()

    def __enter__(self) -> HistoryWriter:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def read_history(path: Path) -> Iterator[ProbeResult]:
    """Stream results back out of a history file.

    Rows that cannot be parsed are skipped rather than raising. A history file
    is append-only and may have been truncated mid-write by the very outage
    being investigated; one damaged final row should not make the preceding
    thousand unreadable.
    """
    required = ("timestamp", "target", "success")
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            # A short row is padded with None rather than raising, so a file
            # truncated mid-write yields rows that look valid and are not.
            if any(not row.get(field) for field in required):
                continue
            try:
                success = row["success"] == "1"
                rtt_raw = row["rtt_ms"]
                yield ProbeResult(
                    target=row["target"],
                    timestamp=float(row["timestamp"]),
                    success=success,
                    rtt_ms=float(rtt_raw) if success and rtt_raw else None,
                    error=(row.get("error") or None),
                )
            except (KeyError, ValueError):
                continue
