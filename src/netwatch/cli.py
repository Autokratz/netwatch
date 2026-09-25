"""Command line entry point.

Exit codes are part of the interface, because this is meant to be run from cron
and from CI:

    0  everything within thresholds
    1  at least one target was firing when the run ended
    2  the configuration could not be loaded

``check`` exists for exactly that: run a fixed number of ticks, print the
table, and exit non-zero if anything breached. That makes it usable as a
post-change gate, not only as a dashboard.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from types import FrameType

from netwatch import __version__
from netwatch.alerting import AlertState
from netwatch.config import Config, ConfigError, load_config
from netwatch.report import render_table, summarise_history
from netwatch.runner import Monitor
from netwatch.storage import HistoryWriter, read_history

EXIT_OK = 0
EXIT_ALERTING = 1
EXIT_CONFIG = 2


@dataclass(slots=True)
class _Interrupt:
    """Records a stop request, and gets out of the way if asked twice.

    A tick may be mid-write to the history, so the first Ctrl-C only sets a
    flag that the loop checks between passes. A second one restores Python's
    default handler, so an impatient user is not stuck with a monitor that
    ignores them.
    """

    requested: bool = False

    def handle(self, signum: int, frame: FrameType | None) -> None:
        if self.requested:
            signal.signal(signal.SIGINT, signal.default_int_handler)
            raise KeyboardInterrupt
        self.requested = True

    def wait(self, seconds: float) -> None:
        """Sleep, but notice an interrupt rather than serving the full term.

        time.sleep resumes for its whole remaining duration once a handler
        returns (PEP 475), so a plain sleep would swallow Ctrl-C for a full
        interval. Short slices keep it responsive.
        """
        deadline = time.perf_counter() + seconds
        while not self.requested:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return
            time.sleep(min(0.2, remaining))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="netwatch",
        description="Threshold monitoring for network targets, with hysteresis.",
    )
    parser.add_argument("--version", action="version", version=f"netwatch {__version__}")

    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="monitor continuously until interrupted")
    run.add_argument("-c", "--config", type=Path, required=True)
    run.add_argument("-n", "--ticks", type=int, default=None, help="stop after N passes")
    run.add_argument("-q", "--quiet", action="store_true", help="alerts only, no table")

    check = sub.add_parser("check", help="run a fixed number of passes and exit non-zero on breach")
    check.add_argument("-c", "--config", type=Path, required=True)
    check.add_argument("-n", "--ticks", type=int, default=5)

    report = sub.add_parser("report", help="summarise a history file")
    report.add_argument("history", type=Path)

    return parser


def _load(path: Path) -> Config:
    """Load the config, or exit 2 with the reason on stderr."""
    try:
        return load_config(path)
    except ConfigError as exc:
        print(f"netwatch: {exc}", file=sys.stderr)
        raise SystemExit(EXIT_CONFIG) from exc


def _firing(monitor: Monitor) -> list[str]:
    return sorted(
        name for name, state in monitor.alert_states().items() if state is AlertState.FIRING
    )


def cmd_run(args: argparse.Namespace) -> int:
    config = _load(args.config)
    interrupt = _Interrupt()
    previous = signal.signal(signal.SIGINT, interrupt.handle)

    history: HistoryWriter | None = None
    try:
        if config.history_path is not None:
            try:
                history = HistoryWriter(config.history_path)
            except OSError as exc:
                print(
                    f"netwatch: cannot write history to {config.history_path}: {exc}",
                    file=sys.stderr,
                )
                return EXIT_CONFIG
            print(f"history -> {history.path}", file=sys.stderr)

        monitor = Monitor(config, history=history, sleep=interrupt.wait)

        for outcome in monitor.run(
            config.interval_s, args.ticks, should_stop=lambda: interrupt.requested
        ):
            for event in outcome.events:
                print(event.format_line(), flush=True)
            if not args.quiet:
                print(render_table(outcome.stats, monitor.alert_states()), flush=True)
                print(flush=True)

        if interrupt.requested:
            print("interrupted, stopped after the current pass", file=sys.stderr)
    finally:
        signal.signal(signal.SIGINT, previous)
        if history is not None:
            history.close()

    return EXIT_ALERTING if _firing(monitor) else EXIT_OK


def cmd_check(args: argparse.Namespace) -> int:
    config = _load(args.config)
    monitor = Monitor(config)
    deque(monitor.run(interval_s=config.interval_s, ticks=args.ticks), maxlen=0)

    print(render_table(monitor.current_stats(), monitor.alert_states()))

    firing = _firing(monitor)
    if firing:
        print(f"\nFIRING: {', '.join(firing)}", file=sys.stderr)
        return EXIT_ALERTING
    return EXIT_OK


def cmd_report(args: argparse.Namespace) -> int:
    if not args.history.exists():
        print(f"netwatch: no such history file: {args.history}", file=sys.stderr)
        return EXIT_CONFIG
    print(render_table(summarise_history(read_history(args.history))))
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {"run": cmd_run, "check": cmd_check, "report": cmd_report}
    return handlers[args.command](args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
