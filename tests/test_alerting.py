"""Hysteresis is the part of this tool most likely to be wrong in a way nobody
notices, so it gets the most tests."""

from __future__ import annotations

import pytest

from netwatch.alerting import AlertEngine, AlertState, Thresholds, evaluate
from netwatch.window import WindowStats


def stats(
    *,
    target: str = "gw",
    samples: int = 10,
    successes: int = 10,
    loss_pct: float = 0.0,
    p95: float | None = 20.0,
) -> WindowStats:
    return WindowStats(
        target=target,
        samples=samples,
        successes=successes,
        failures=samples - successes,
        loss_pct=loss_pct,
        rtt_avg_ms=p95,
        rtt_p95_ms=p95,
        rtt_max_ms=p95,
        last_error=None,
    )


class TestThresholdsValidation:
    def test_rejects_a_target_with_no_thresholds(self):
        with pytest.raises(ValueError, match="at least one threshold"):
            Thresholds()

    @pytest.mark.parametrize("value", [0, -1])
    def test_rejects_non_positive_for_breaches(self, value):
        with pytest.raises(ValueError, match="for_breaches"):
            Thresholds(loss_pct=5.0, for_breaches=value)

    @pytest.mark.parametrize("value", [-0.1, 100.1])
    def test_rejects_loss_outside_zero_to_hundred(self, value):
        with pytest.raises(ValueError, match="loss_pct"):
            Thresholds(loss_pct=value)


class TestEvaluate:
    def test_empty_window_is_not_a_breach(self):
        empty = WindowStats("gw", 0, 0, 0, 0.0, None, None, None, None)
        assert evaluate(empty, Thresholds(loss_pct=0.0)) == ()

    def test_loss_at_the_threshold_is_not_a_breach(self):
        # Strictly greater than, so a threshold of 10% tolerates exactly 10%.
        assert evaluate(stats(loss_pct=10.0), Thresholds(loss_pct=10.0)) == ()

    def test_loss_above_the_threshold_breaches(self):
        reasons = evaluate(stats(loss_pct=10.1, samples=10, successes=9), Thresholds(loss_pct=10.0))
        assert len(reasons) == 1
        assert "loss" in reasons[0]

    def test_latency_above_the_threshold_breaches(self):
        reasons = evaluate(stats(p95=150.0), Thresholds(rtt_p95_ms=100.0))
        assert any("p95" in r for r in reasons)

    def test_both_thresholds_produce_two_reasons(self):
        reasons = evaluate(
            stats(loss_pct=50.0, samples=10, successes=5, p95=400.0),
            Thresholds(rtt_p95_ms=100.0, loss_pct=10.0),
        )
        assert len(reasons) == 2

    def test_total_loss_is_not_double_counted(self):
        """Every sample failed, so p95 is unknown. That is one fault, not two."""
        total_loss = stats(samples=10, successes=0, loss_pct=100.0, p95=None)
        reasons = evaluate(total_loss, Thresholds(rtt_p95_ms=100.0, loss_pct=10.0))
        assert len(reasons) == 1
        assert "loss" in reasons[0]

    def test_total_loss_reports_when_only_latency_is_configured(self):
        total_loss = stats(samples=10, successes=0, loss_pct=100.0, p95=None)
        reasons = evaluate(total_loss, Thresholds(rtt_p95_ms=100.0))
        assert reasons == ("no successful samples in window",)


class TestHysteresis:
    @pytest.fixture
    def engine(self) -> AlertEngine:
        return AlertEngine(
            {"gw": Thresholds(rtt_p95_ms=100.0, loss_pct=10.0, for_breaches=3, clear_after=5)}
        )

    def test_does_not_fire_before_the_breach_count_is_reached(self, engine):
        breach = stats(p95=500.0)
        assert engine.observe(breach) is None
        assert engine.observe(breach) is None
        assert engine.state_of("gw") is AlertState.OK

    def test_fires_exactly_on_the_third_consecutive_breach(self, engine):
        breach = stats(p95=500.0)
        engine.observe(breach)
        engine.observe(breach)
        event = engine.observe(breach)

        assert event is not None
        assert event.state is AlertState.FIRING
        assert engine.state_of("gw") is AlertState.FIRING

    def test_a_single_good_sample_resets_the_breach_run(self, engine):
        breach, good = stats(p95=500.0), stats(p95=20.0)
        engine.observe(breach)
        engine.observe(breach)
        engine.observe(good)  # resets the counter
        engine.observe(breach)
        engine.observe(breach)

        assert engine.state_of("gw") is AlertState.OK, "should need three in a row again"

    def test_stays_silent_while_it_remains_firing(self, engine):
        breach = stats(p95=500.0)
        for _ in range(3):
            engine.observe(breach)

        further = [engine.observe(breach) for _ in range(10)]
        assert all(e is None for e in further), "transitions only, never repeats"

    def test_does_not_resolve_before_the_clear_count(self, engine):
        breach, good = stats(p95=500.0), stats(p95=20.0)
        for _ in range(3):
            engine.observe(breach)

        for _ in range(4):
            assert engine.observe(good) is None
        assert engine.state_of("gw") is AlertState.FIRING

    def test_resolves_on_the_fifth_consecutive_good_sample(self, engine):
        breach, good = stats(p95=500.0), stats(p95=20.0)
        for _ in range(3):
            engine.observe(breach)
        for _ in range(4):
            engine.observe(good)

        event = engine.observe(good)
        assert event is not None
        assert event.state is AlertState.OK
        assert event.reasons == ()
        assert engine.state_of("gw") is AlertState.OK

    def test_a_breach_during_recovery_restarts_the_clear_count(self, engine):
        breach, good = stats(p95=500.0), stats(p95=20.0)
        for _ in range(3):
            engine.observe(breach)
        for _ in range(4):
            engine.observe(good)

        engine.observe(breach)  # recovery interrupted
        for _ in range(4):
            engine.observe(good)
        assert engine.state_of("gw") is AlertState.FIRING, "must start the five again"

    def test_flapping_input_produces_no_events_at_all(self, engine):
        """Alternating good/bad never reaches three or five in a row, so a
        flapping link stays quiet instead of generating a page every minute."""
        breach, good = stats(p95=500.0), stats(p95=20.0)
        events = [engine.observe(s) for _ in range(20) for s in (breach, good)]
        assert all(e is None for e in events)
        assert engine.state_of("gw") is AlertState.OK


class TestAlertEvent:
    def test_firing_line_names_the_target_and_the_reason(self):
        engine = AlertEngine({"gw": Thresholds(rtt_p95_ms=100.0, for_breaches=1)})
        event = engine.observe(stats(p95=500.0))
        assert event is not None

        line = event.format_line()
        assert "FIRING" in line
        assert "gw" in line
        assert "p95" in line

    def test_resolved_line_reads_sensibly_with_no_reasons(self):
        engine = AlertEngine({"gw": Thresholds(rtt_p95_ms=100.0, for_breaches=1, clear_after=1)})
        engine.observe(stats(p95=500.0))
        event = engine.observe(stats(p95=10.0))

        assert event is not None
        assert "RESOLVED" in event.format_line()
        assert "back within limits" in event.format_line()

    def test_unknown_target_raises_rather_than_silently_ignoring(self):
        engine = AlertEngine({"gw": Thresholds(loss_pct=1.0)})
        with pytest.raises(KeyError):
            engine.observe(stats(target="not-configured"))
