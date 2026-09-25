from __future__ import annotations

import subprocess

import pytest

from netwatch.probes import (
    IcmpProbe,
    ProbeResult,
    TcpProbe,
    build_probe,
    parse_ping_rtt,
)

IPUTILS = """PING 10.0.0.1 (10.0.0.1) 56(84) bytes of data.
64 bytes from 10.0.0.1: icmp_seq=1 ttl=64 time=0.412 ms

--- 10.0.0.1 ping statistics ---
1 packets transmitted, 1 received, 0% packet loss, time 0ms
"""

BSD = """PING 1.1.1.1 (1.1.1.1): 56 data bytes
64 bytes from 1.1.1.1: icmp_seq=0 ttl=57 time=12.345 ms
"""

BUSYBOX = "64 bytes from 10.0.0.1: seq=0 ttl=64 time=1.5 ms\n"

SUB_MILLISECOND = "64 bytes from 10.0.0.1: icmp_seq=1 ttl=64 time<1 ms\n"

UNREACHABLE = """PING 10.99.99.99 (10.99.99.99) 56(84) bytes of data.

--- 10.99.99.99 ping statistics ---
1 packets transmitted, 0 received, 100% packet loss, time 0ms
"""


class TestParsePingRtt:
    @pytest.mark.parametrize(
        ("output", "expected"),
        [(IPUTILS, 0.412), (BSD, 12.345), (BUSYBOX, 1.5), (SUB_MILLISECOND, 1.0)],
        ids=["iputils", "bsd", "busybox", "sub-millisecond"],
    )
    def test_parses_each_ping_dialect(self, output, expected):
        assert parse_ping_rtt(output) == pytest.approx(expected)

    def test_returns_none_when_nothing_replied(self):
        assert parse_ping_rtt(UNREACHABLE) is None

    def test_returns_none_for_empty_output(self):
        assert parse_ping_rtt("") is None

    def test_takes_the_first_reply_when_several_are_present(self):
        assert parse_ping_rtt(IPUTILS + BSD) == pytest.approx(0.412)


class TestIcmpProbe:
    def test_successful_ping_produces_an_rtt(self, monkeypatch):
        monkeypatch.setattr("netwatch.probes.shutil.which", lambda _: "/bin/ping")
        monkeypatch.setattr(
            "netwatch.probes.subprocess.run",
            lambda *a, **k: subprocess.CompletedProcess(a[0], 0, IPUTILS, ""),
        )
        result = IcmpProbe("gw", "10.0.0.1").measure()

        assert result.success
        assert result.rtt_ms == pytest.approx(0.412)
        assert result.error is None

    def test_non_zero_exit_is_a_failure_with_the_last_line_as_the_reason(self, monkeypatch):
        monkeypatch.setattr("netwatch.probes.shutil.which", lambda _: "/bin/ping")
        monkeypatch.setattr(
            "netwatch.probes.subprocess.run",
            lambda *a, **k: subprocess.CompletedProcess(a[0], 1, UNREACHABLE, ""),
        )
        result = IcmpProbe("gw", "10.99.99.99").measure()

        assert not result.success
        assert result.rtt_ms is None
        assert result.error

    def test_exit_zero_with_no_timing_is_still_a_failure(self, monkeypatch):
        """Trusting the exit code over the actual output is how phantom
        measurements get into a history file."""
        monkeypatch.setattr("netwatch.probes.shutil.which", lambda _: "/bin/ping")
        monkeypatch.setattr(
            "netwatch.probes.subprocess.run",
            lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "no timing here", ""),
        )
        result = IcmpProbe("gw", "10.0.0.1").measure()

        assert not result.success
        assert result.error == "no timing in ping output"

    def test_timeout_is_reported_not_raised(self, monkeypatch):
        def explode(*a, **k):
            raise subprocess.TimeoutExpired(cmd="ping", timeout=3)

        monkeypatch.setattr("netwatch.probes.shutil.which", lambda _: "/bin/ping")
        monkeypatch.setattr("netwatch.probes.subprocess.run", explode)

        result = IcmpProbe("gw", "10.0.0.1").measure()
        assert not result.success
        assert result.error == "timed out"

    def test_missing_ping_binary_is_reported_not_raised(self, monkeypatch):
        monkeypatch.setattr("netwatch.probes.shutil.which", lambda _: None)
        result = IcmpProbe("gw", "10.0.0.1").measure()

        assert not result.success
        assert "ping not found" in (result.error or "")

    def test_never_raises_whatever_the_subprocess_does(self, monkeypatch):
        def explode(*a, **k):
            raise OSError("no such file")

        monkeypatch.setattr("netwatch.probes.shutil.which", lambda _: "/bin/ping")
        monkeypatch.setattr("netwatch.probes.subprocess.run", explode)

        result = IcmpProbe("gw", "10.0.0.1").measure()
        assert not result.success


class TestTcpProbe:
    @pytest.mark.integration
    def test_refused_connection_is_a_failure(self):
        # Port 1 on the loopback: nothing listens, and the refusal is immediate.
        result = TcpProbe("closed", "127.0.0.1", 1, timeout_s=1.0).measure()
        assert not result.success
        assert result.rtt_ms is None
        assert result.error

    @pytest.mark.integration
    def test_successful_connection_is_timed(self):
        import socket
        import threading

        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        threading.Thread(target=lambda: server.accept(), daemon=True).start()

        try:
            result = TcpProbe("local", "127.0.0.1", port, timeout_s=2.0).measure()
            assert result.success
            assert result.rtt_ms is not None
            assert result.rtt_ms >= 0.0
        finally:
            server.close()


class TestBuildProbe:
    def test_builds_an_icmp_probe(self):
        assert isinstance(build_probe("gw", "icmp", "10.0.0.1"), IcmpProbe)

    def test_builds_a_tcp_probe(self):
        assert isinstance(build_probe("web", "tcp", "example.com", 443), TcpProbe)

    def test_tcp_without_a_port_is_rejected(self):
        with pytest.raises(ValueError, match="needs a port"):
            build_probe("web", "tcp", "example.com")

    def test_unknown_kind_is_rejected_and_names_the_target(self):
        with pytest.raises(ValueError, match="unknown probe kind"):
            build_probe("weird", "carrier-pigeon", "somewhere")


class TestProbeResultInvariants:
    def test_success_without_an_rtt_is_rejected(self):
        with pytest.raises(ValueError, match="must carry an rtt_ms"):
            ProbeResult("gw", 0.0, success=True)

    def test_failure_carrying_an_rtt_is_rejected(self):
        with pytest.raises(ValueError, match="must not carry an rtt_ms"):
            ProbeResult("gw", 0.0, success=False, rtt_ms=5.0)
