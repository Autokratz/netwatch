from __future__ import annotations

import pytest

from netwatch.alerting import AlertState, Thresholds
from netwatch.config import Config, TargetConfig
from netwatch.report import render_table, summarise_history
from netwatch.runner import Monitor
from netwatch.storage import HistoryWriter, read_history
from netwatch.window import RollingWindow, WindowStats
from tests.conftest import FakeProbe, RaisingProbe, fail, ok


class TestHistoryWriter:
    def test_round_trips_results(self, tmp_path):
        path = tmp_path / "history.csv"
        with HistoryWriter(path) as writer:
            writer.append(ok("gw", 12.5, ts=1000.0))
            writer.append(fail("gw", "timed out", ts=1001.0))

        recovered = list(read_history(path))
        assert len(recovered) == 2
        assert recovered[0].success
        assert recovered[0].rtt_ms == pytest.approx(12.5)
        assert not recovered[1].success
        assert recovered[1].error == "timed out"

    def test_creates_parent_directories(self, tmp_path):
        path = tmp_path / "deep" / "nested" / "history.csv"
        with HistoryWriter(path) as writer:
            writer.append(ok("gw", 1.0))
        assert path.exists()

    def test_appending_to_an_existing_file_does_not_repeat_the_header(self, tmp_path):
        path = tmp_path / "history.csv"
        with HistoryWriter(path) as w:
            w.append(ok("gw", 1.0))
        with HistoryWriter(path) as w:
            w.append(ok("gw", 2.0))

        header_rows = [
            line for line in path.read_text().splitlines() if line.startswith("timestamp,")
        ]
        assert len(header_rows) == 1
        assert len(list(read_history(path))) == 2

    def test_flushes_each_row_so_a_crash_keeps_the_history(self, tmp_path):
        """The history exists for the outage that killed the process. Buffering
        without flushing loses exactly the rows that matter."""
        path = tmp_path / "history.csv"
        writer = HistoryWriter(path)
        writer.append(ok("gw", 1.0))
        # deliberately not closed
        assert len(list(read_history(path))) == 1
        writer.close()

    def test_a_damaged_final_row_does_not_discard_the_rest(self, tmp_path):
        path = tmp_path / "history.csv"
        with HistoryWriter(path) as w:
            w.append(ok("gw", 1.0))
            w.append(ok("gw", 2.0))
        with path.open("a", encoding="utf-8") as fh:
            fh.write("truncated,row,with,bad\n")

        assert len(list(read_history(path))) == 2


class TestRenderTable:
    def test_empty_input_says_so_rather_than_printing_a_bare_header(self):
        assert render_table([]) == "no data"

    def test_columns_align(self):
        """The old assertion here was a tautology that passed for any input,
        including a completely misaligned table. Pin a real column instead."""
        rows = [
            WindowStats("gw", 10, 10, 0, 0.0, 1.5, 2.0, 2.5, None),
            WindowStats("a-very-long-target-name", 10, 9, 1, 10.0, 100.0, 200.0, 250.0, "x"),
        ]
        lines = render_table(rows).splitlines()
        assert "TARGET" in lines[0]

        # every row must start its SAMPLES column at the same offset
        header_offset = lines[0].index("SAMPLES")
        for row, expected in zip(lines[2:], ["10", "10"], strict=True):
            assert row[: header_offset + len("SAMPLES")].endswith(expected), row

    def test_missing_latency_renders_as_a_dash(self):
        rows = [WindowStats("gw", 5, 0, 5, 100.0, None, None, None, "down")]
        assert "-" in render_table(rows)

    def test_state_column_reflects_the_engine(self):
        rows = [WindowStats("gw", 5, 5, 0, 0.0, 1.0, 1.0, 1.0, None)]
        out = render_table(rows, {"gw": AlertState.FIRING})
        assert "FIRING" in out


class TestSummariseHistory:
    def test_groups_by_target(self, tmp_path):
        path = tmp_path / "history.csv"
        with HistoryWriter(path) as w:
            w.append(ok("gw", 10.0))
            w.append(ok("api", 100.0))
            w.append(fail("gw"))

        summaries = {s.target: s for s in summarise_history(read_history(path))}
        assert set(summaries) == {"api", "gw"}
        assert summaries["gw"].samples == 2
        assert summaries["gw"].loss_pct == pytest.approx(50.0)


def _config(window: int = 5, for_breaches: int = 3, clear_after: int = 5) -> Config:
    return Config(
        interval_s=0.0,
        window_size=window,
        targets=(
            TargetConfig(
                name="gw",
                kind="icmp",
                host="10.0.0.1",
                thresholds=Thresholds(
                    rtt_p95_ms=100.0, for_breaches=for_breaches, clear_after=clear_after
                ),
            ),
        ),
    )


class TestMonitor:
    def test_tick_records_a_sample_per_target(self):
        probe = FakeProbe("gw", [ok("gw", 10.0)])
        monitor = Monitor(_config(), probes=[probe], sleep=lambda _: None)

        outcome = monitor.tick()
        assert len(outcome.results) == 1
        assert outcome.stats[0].samples == 1
        assert outcome.events == ()

    def test_a_probe_that_raises_is_recorded_not_propagated(self):
        """The contract says probes must not raise. The loop must survive one
        that does anyway, or a single bad target takes down the monitor."""
        monitor = Monitor(_config(), probes=[RaisingProbe("gw")], sleep=lambda _: None)

        outcome = monitor.tick()
        assert len(outcome.results) == 1
        assert not outcome.results[0].success
        assert "probe raised" in (outcome.results[0].error or "")

    def test_fires_after_the_configured_number_of_breaches(self):
        probe = FakeProbe("gw", [ok("gw", 500.0)])
        monitor = Monitor(_config(for_breaches=3), probes=[probe], sleep=lambda _: None)

        events = [e for _ in range(3) for e in monitor.tick().events]
        assert len(events) == 1
        assert events[0].state is AlertState.FIRING

    def test_run_executes_the_requested_number_of_ticks(self):
        probe = FakeProbe("gw", [ok("gw", 10.0)])
        monitor = Monitor(_config(), probes=[probe], sleep=lambda _: None)

        outcomes = list(monitor.run(interval_s=0.0, ticks=4))
        assert len(outcomes) == 4
        assert probe.calls == 4

    def test_run_does_not_sleep_after_the_final_tick(self):
        slept: list[float] = []
        probe = FakeProbe("gw", [ok("gw", 10.0)])
        monitor = Monitor(_config(), probes=[probe], sleep=slept.append)

        list(monitor.run(interval_s=5.0, ticks=3))
        assert len(slept) == 2, "n ticks means n-1 sleeps"

    def test_history_receives_every_sample(self, tmp_path):
        path = tmp_path / "history.csv"
        probe = FakeProbe("gw", [ok("gw", 10.0)])
        with HistoryWriter(path) as history:
            monitor = Monitor(_config(), probes=[probe], history=history, sleep=lambda _: None)
            list(monitor.run(interval_s=0.0, ticks=3))

        assert len(list(read_history(path))) == 3

    def test_window_never_exceeds_its_configured_size(self):
        probe = FakeProbe("gw", [ok("gw", 10.0)])
        monitor = Monitor(_config(window=3), probes=[probe], sleep=lambda _: None)

        list(monitor.run(interval_s=0.0, ticks=50))
        assert monitor.current_stats()[0].samples == 3


class TestRollingWindowIntegration:
    def test_stats_survive_a_long_mixed_run(self):
        window = RollingWindow("gw", 100)
        for i in range(500):
            window.add(ok("gw", float(i % 50)) if i % 7 else fail("gw"))

        s = window.stats()
        assert s.samples == 100
        assert 0 < s.loss_pct < 100
        assert s.rtt_p95_ms is not None


class TestHistoryIsRobust:
    def test_a_row_truncated_mid_write_is_skipped(self, tmp_path):
        """csv.DictReader pads a short row with None rather than raising, so
        this used to yield ProbeResult(target=None) and crash the report."""
        path = tmp_path / "history.csv"
        with HistoryWriter(path) as w:
            w.append(ok("gw", 10.0))
            w.append(ok("gw", 12.0))
        with path.open("a", encoding="utf-8") as fh:
            fh.write("3.0")  # power cut mid-row

        recovered = list(read_history(path))
        assert len(recovered) == 2
        assert all(r.target == "gw" for r in recovered)

    def test_a_row_truncated_after_the_target_is_not_read_as_a_failure(self, tmp_path):
        """A short row used to become a fabricated failed sample, silently
        inflating loss_pct in the report."""
        path = tmp_path / "history.csv"
        with HistoryWriter(path) as w:
            w.append(ok("gw", 10.0))
        with path.open("a", encoding="utf-8") as fh:
            fh.write("2.0,iso,gw\n")

        recovered = list(read_history(path))
        assert len(recovered) == 1
        assert recovered[0].success

    def test_summarise_survives_a_truncated_file(self, tmp_path):
        path = tmp_path / "history.csv"
        with HistoryWriter(path) as w:
            w.append(ok("gw", 10.0))
            w.append(ok("api", 20.0))
        with path.open("a", encoding="utf-8") as fh:
            fh.write("9.9")

        summaries = summarise_history(read_history(path))
        assert [s.target for s in summaries] == ["api", "gw"]


class TestHistoryFailureDoesNotStopTheMonitor:
    def test_a_write_error_does_not_stop_the_tick_or_corrupt_the_sample(self, tmp_path, capsys):
        """A full disk is not a network fault. The probe result must stay
        truthful, or loss climbs and an alert fires for the wrong reason."""

        class Exploding(HistoryWriter):
            def append(self, result):
                raise OSError("No space left on device")

        writer = Exploding(tmp_path / "h.csv")
        probe = FakeProbe("gw", [ok("gw", 10.0)])
        monitor = Monitor(_config(), probes=[probe], history=writer, sleep=lambda _: None)

        outcome = monitor.tick()
        assert outcome.results[0].success
        assert outcome.results[0].rtt_ms == pytest.approx(10.0)
        assert outcome.stats[0].loss_pct == 0.0
        assert "history write failed" in capsys.readouterr().err
        writer.close()

    def test_a_broken_sink_is_dropped_rather_than_retried_every_tick(self, tmp_path):
        attempts = {"n": 0}

        class Exploding(HistoryWriter):
            def append(self, result):
                attempts["n"] += 1
                raise OSError("No space left on device")

        writer = Exploding(tmp_path / "h.csv")
        probe = FakeProbe("gw", [ok("gw", 10.0)])
        monitor = Monitor(_config(), probes=[probe], history=writer, sleep=lambda _: None)

        list(monitor.run(interval_s=0.0, ticks=5))
        assert attempts["n"] == 1
        writer.close()
