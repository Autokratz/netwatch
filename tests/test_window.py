from __future__ import annotations

import pytest

from netwatch.probes import ProbeResult
from netwatch.window import RollingWindow, percentile
from tests.conftest import fail, ok


class TestPercentile:
    def test_p95_of_a_single_value_is_that_value(self):
        assert percentile([42.0], 95.0) == 42.0

    def test_p100_is_the_maximum(self):
        assert percentile([1.0, 9.0, 5.0], 100.0) == 9.0

    def test_p50_uses_nearest_rank(self):
        # ceil(0.5 * 4) = 2 -> the 2nd of [1,2,3,4]
        assert percentile([1.0, 2.0, 3.0, 4.0], 50.0) == 2.0

    def test_is_order_independent(self):
        assert percentile([5.0, 1.0, 3.0], 95.0) == percentile([1.0, 3.0, 5.0], 95.0)

    def test_empty_sample_raises(self):
        with pytest.raises(ValueError, match="empty sample"):
            percentile([], 95.0)

    @pytest.mark.parametrize("pct", [0, -1, 101])
    def test_out_of_range_percentile_raises(self, pct):
        with pytest.raises(ValueError, match="must be in"):
            percentile([1.0], pct)


class TestRollingWindow:
    def test_rejects_a_zero_size(self):
        with pytest.raises(ValueError, match="at least 1"):
            RollingWindow("gw", 0)

    def test_evicts_the_oldest_once_full(self):
        window = RollingWindow("gw", 3)
        for rtt in (1.0, 2.0, 3.0, 4.0):
            window.add(ok("gw", rtt))

        assert len(window) == 3
        assert window.stats().rtt_max_ms == 4.0
        assert window.stats().rtt_avg_ms == pytest.approx(3.0)

    def test_never_grows_past_capacity(self):
        window = RollingWindow("gw", 5)
        for i in range(1000):
            window.add(ok("gw", float(i)))
        assert len(window) == 5 == window.capacity

    def test_rejects_a_result_for_a_different_target(self):
        window = RollingWindow("gw", 3)
        with pytest.raises(ValueError, match="does not belong"):
            window.add(ok("other", 1.0))

    def test_empty_window_reports_no_statistics(self):
        s = RollingWindow("gw", 3).stats()
        assert s.is_empty
        assert s.samples == 0
        assert s.rtt_avg_ms is None
        assert s.loss_pct == 0.0

    def test_loss_percentage(self):
        window = RollingWindow("gw", 4)
        window.add(ok("gw", 10.0))
        window.add(fail("gw"))
        window.add(ok("gw", 20.0))
        window.add(fail("gw"))

        s = window.stats()
        assert s.samples == 4
        assert s.successes == 2
        assert s.failures == 2
        assert s.loss_pct == pytest.approx(50.0)

    def test_statistics_ignore_failed_samples(self):
        window = RollingWindow("gw", 3)
        window.add(ok("gw", 10.0))
        window.add(fail("gw"))
        window.add(ok("gw", 30.0))

        s = window.stats()
        assert s.rtt_avg_ms == pytest.approx(20.0), "the failure must not count as 0ms"
        assert s.rtt_max_ms == 30.0

    def test_all_failures_leaves_latency_undefined(self):
        window = RollingWindow("gw", 2)
        window.add(fail("gw"))
        window.add(fail("gw"))

        s = window.stats()
        assert s.loss_pct == 100.0
        assert s.rtt_avg_ms is None
        assert s.rtt_p95_ms is None

    def test_reports_the_most_recent_error(self):
        window = RollingWindow("gw", 3)
        window.add(fail("gw", "first"))
        window.add(ok("gw", 1.0))
        window.add(fail("gw", "most recent"))

        assert window.stats().last_error == "most recent"

    def test_clear_empties_the_window(self):
        window = RollingWindow("gw", 3)
        window.add(ok("gw", 1.0))
        window.clear()
        assert len(window) == 0


class TestProbeResultInvariants:
    def test_success_without_an_rtt_is_rejected(self):
        with pytest.raises(ValueError, match="must carry an rtt_ms"):
            ProbeResult("gw", 0.0, success=True)

    def test_failure_carrying_an_rtt_is_rejected(self):
        with pytest.raises(ValueError, match="must not carry an rtt_ms"):
            ProbeResult("gw", 0.0, success=False, rtt_ms=5.0)
