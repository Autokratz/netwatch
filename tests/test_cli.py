"""The exit codes are the CLI's contract with cron and CI, so they are tested
as behaviour rather than left to manual checking."""

from __future__ import annotations

import signal
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from netwatch.cli import EXIT_ALERTING, EXIT_CONFIG, EXIT_OK, _Interrupt, build_parser, main
from netwatch.storage import HistoryWriter
from tests.conftest import fail, ok

GOOD_CONFIG = """
interval_s = 0.01
window_size = 3

[[target]]
name = "loopback"
kind = "tcp"
host = "127.0.0.1"
port = 1
timeout_s = 0.2
loss_pct = 99.0
"""

ALWAYS_BREACHING = """
interval_s = 0.01
window_size = 3

[[target]]
name = "closed-port"
kind = "tcp"
host = "127.0.0.1"
port = 1
timeout_s = 0.2
loss_pct = 0.0
for_breaches = 1
clear_after = 1
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Callable[[str], Path]:
    def write(text: str) -> Path:
        path = tmp_path / "netwatch.toml"
        path.write_text(text, encoding="utf-8")
        return path

    return write


class TestParser:
    def test_requires_a_subcommand(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args([])

    def test_run_requires_a_config(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["run"])

    def test_check_defaults_to_five_ticks(self):
        args = build_parser().parse_args(["check", "-c", "x.toml"])
        assert args.ticks == 5

    def test_version_exits_zero(self):
        with pytest.raises(SystemExit) as exc:
            build_parser().parse_args(["--version"])
        assert exc.value.code == 0


class TestExitCodes:
    def test_unreadable_config_exits_two(self, tmp_path, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["check", "-c", str(tmp_path / "absent.toml")])
        assert exc.value.code == EXIT_CONFIG
        assert "netwatch:" in capsys.readouterr().err

    def test_invalid_config_exits_two(self, config_file, capsys):
        path = config_file("interval_s = -5\n")
        with pytest.raises(SystemExit) as exc:
            main(["check", "-c", str(path)])
        assert exc.value.code == EXIT_CONFIG

    def test_check_exits_one_when_a_target_is_firing(self, config_file, capsys):
        """Port 1 on loopback refuses immediately, so every sample fails and a
        0% loss threshold is breached on the first evaluation."""
        path = config_file(ALWAYS_BREACHING)
        assert main(["check", "-c", str(path), "-n", "2"]) == EXIT_ALERTING
        assert "FIRING" in capsys.readouterr().err

    def test_check_exits_zero_when_within_thresholds(self, config_file):
        path = config_file(GOOD_CONFIG)
        assert main(["check", "-c", str(path), "-n", "2"]) == EXIT_OK

    def test_run_with_a_tick_limit_terminates(self, config_file):
        path = config_file(GOOD_CONFIG)
        assert main(["run", "-c", str(path), "-n", "2"]) == EXIT_OK

    def test_run_quiet_prints_no_table(self, config_file, capsys):
        path = config_file(GOOD_CONFIG)
        main(["run", "-c", str(path), "-n", "2", "-q"])
        assert "TARGET" not in capsys.readouterr().out


class TestReportCommand:
    def test_missing_history_file_exits_two(self, tmp_path, capsys):
        assert main(["report", str(tmp_path / "nope.csv")]) == EXIT_CONFIG
        assert "no such history file" in capsys.readouterr().err

    def test_summarises_an_existing_history(self, tmp_path, capsys):
        path = tmp_path / "history.csv"
        with HistoryWriter(path) as w:
            w.append(ok("gw", 10.0))
            w.append(fail("gw"))

        assert main(["report", str(path)]) == EXIT_OK
        out = capsys.readouterr().out
        assert "gw" in out
        assert "50.0" in out, "one of two samples failed"


class TestRunWritesHistory:
    def test_history_file_is_created_and_populated(self, tmp_path, config_file):
        history = tmp_path / "out" / "history.csv"
        # A root key must come BEFORE the first [[target]] table, or TOML
        # scoping makes it a key of that target instead.
        path = config_file(f'history_path = "{history.as_posix()}"\n' + GOOD_CONFIG)

        main(["run", "-c", str(path), "-n", "3", "-q"])

        assert history.exists()
        assert len(history.read_text().splitlines()) == 4, "header plus three samples"


class TestInterrupt:
    def test_first_signal_records_intent_without_raising(self):
        """SIGINT must not tear down mid-write; it sets a flag the loop reads
        between passes."""
        interrupt = _Interrupt()
        assert not interrupt.requested

        interrupt.handle(2, None)
        assert interrupt.requested

    def test_second_signal_escalates(self):
        """A user who presses Ctrl-C twice is not asking politely any more."""
        interrupt = _Interrupt()
        interrupt.handle(2, None)

        previous = signal.getsignal(signal.SIGINT)
        try:
            with pytest.raises(KeyboardInterrupt):
                interrupt.handle(2, None)
        finally:
            signal.signal(signal.SIGINT, previous)

    def test_wait_returns_early_once_interrupted(self):
        """time.sleep serves its full term after a handler returns (PEP 475),
        so a plain sleep would swallow Ctrl-C for a whole interval."""
        interrupt = _Interrupt()
        interrupt.requested = True

        started = time.perf_counter()
        interrupt.wait(5.0)
        assert time.perf_counter() - started < 0.5

    def test_run_restores_the_previous_handler(self, config_file):
        original = signal.getsignal(signal.SIGINT)
        main(["run", "-c", str(config_file(GOOD_CONFIG)), "-n", "1", "-q"])
        assert signal.getsignal(signal.SIGINT) is original


class TestConfigFailuresExitTwo:
    def test_an_unwritable_history_path_reports_instead_of_tracing(
        self, tmp_path, config_file, capsys
    ):
        blocked = tmp_path / "blocked"
        blocked.mkdir(mode=0o500)
        path = config_file(
            f'history_path = "{(blocked / "sub" / "h.csv").as_posix()}"\n' + GOOD_CONFIG
        )
        try:
            assert main(["run", "-c", str(path), "-n", "1", "-q"]) == EXIT_CONFIG
            assert "cannot write history" in capsys.readouterr().err
        finally:
            blocked.chmod(0o700)

    def test_an_empty_history_path_is_rejected(self, config_file, capsys):
        path = config_file('history_path = ""\n' + GOOD_CONFIG)
        with pytest.raises(SystemExit) as exc:
            main(["check", "-c", str(path)])
        assert exc.value.code == EXIT_CONFIG
        assert "history_path is empty" in capsys.readouterr().err

    def test_a_target_that_is_not_a_table_is_rejected(self, config_file, capsys):
        path = config_file('target = ["not-a-table"]\n')
        with pytest.raises(SystemExit) as exc:
            main(["check", "-c", str(path)])
        assert exc.value.code == EXIT_CONFIG
        assert "must be a table" in capsys.readouterr().err
