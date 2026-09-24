from __future__ import annotations

from pathlib import Path

import pytest

from netwatch.config import ConfigError, load_config

MINIMAL = """
[[target]]
name = "gateway"
kind = "icmp"
host = "10.0.0.1"
loss_pct = 10.0
"""

FULL = """
interval_s = 5.0
window_size = 20
history_path = "out/history.csv"

[[target]]
name = "gateway"
kind = "icmp"
host = "10.0.0.1"
rtt_p95_ms = 50.0
loss_pct = 2.0
for_breaches = 2
clear_after = 4

[[target]]
name = "api"
kind = "tcp"
host = "api.internal"
port = 443
timeout_s = 3.0
rtt_p95_ms = 250.0
"""


def write(tmp_path: Path, text: str, name: str = "netwatch.toml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


class TestDefaults:
    def test_minimal_config_gets_sensible_defaults(self, tmp_path):
        cfg = load_config(write(tmp_path, MINIMAL))

        assert cfg.interval_s == 10.0
        assert cfg.window_size == 10
        assert cfg.history_path is None
        assert len(cfg.targets) == 1

        target = cfg.targets[0]
        assert target.timeout_s == 2.0
        assert target.thresholds.for_breaches == 3
        assert target.thresholds.clear_after == 5


class TestFullConfig:
    def test_parses_every_field(self, tmp_path):
        cfg = load_config(write(tmp_path, FULL))

        assert cfg.interval_s == 5.0
        assert cfg.window_size == 20
        assert cfg.history_path is not None
        assert cfg.history_path.name == "history.csv"

        gateway, api = cfg.targets
        assert gateway.kind == "icmp"
        assert gateway.port is None
        assert gateway.thresholds.for_breaches == 2

        assert api.kind == "tcp"
        assert api.port == 443
        assert api.timeout_s == 3.0

    def test_thresholds_by_target_maps_every_target(self, tmp_path):
        cfg = load_config(write(tmp_path, FULL))
        mapping = cfg.thresholds_by_target()
        assert set(mapping) == {"gateway", "api"}


class TestValidation:
    def test_missing_file_names_the_path(self, tmp_path):
        with pytest.raises(ConfigError, match="cannot read"):
            load_config(tmp_path / "absent.toml")

    def test_malformed_toml_is_reported(self, tmp_path):
        with pytest.raises(ConfigError):
            load_config(write(tmp_path, "this is not = = toml"))

    def test_no_targets_is_rejected(self, tmp_path):
        with pytest.raises(ConfigError, match="at least one"):
            load_config(write(tmp_path, "interval_s = 5.0\n"))

    def test_duplicate_target_names_are_rejected(self, tmp_path):
        text = MINIMAL + MINIMAL
        with pytest.raises(ConfigError, match="duplicate target name"):
            load_config(write(tmp_path, text))

    def test_unknown_kind_names_the_target(self, tmp_path):
        text = MINIMAL.replace('kind = "icmp"', 'kind = "smoke-signal"')
        with pytest.raises(ConfigError, match="gateway"):
            load_config(write(tmp_path, text))

    def test_tcp_without_a_port_is_rejected(self, tmp_path):
        text = MINIMAL.replace('kind = "icmp"', 'kind = "tcp"')
        with pytest.raises(ConfigError, match="missing required key 'port'"):
            load_config(write(tmp_path, text))

    def test_icmp_with_a_port_is_rejected(self, tmp_path):
        """Silently ignoring it would let a typo change what is monitored."""
        text = MINIMAL + "port = 443\n"
        with pytest.raises(ConfigError, match="must not specify a port"):
            load_config(write(tmp_path, text))

    @pytest.mark.parametrize("port", [0, 65536, -1])
    def test_out_of_range_port_is_rejected(self, tmp_path, port):
        text = MINIMAL.replace('kind = "icmp"', 'kind = "tcp"') + f"port = {port}\n"
        with pytest.raises(ConfigError, match="between 1 and 65535"):
            load_config(write(tmp_path, text))

    def test_target_with_no_thresholds_is_rejected(self, tmp_path):
        text = MINIMAL.replace("loss_pct = 10.0", "")
        with pytest.raises(ConfigError, match="at least one threshold"):
            load_config(write(tmp_path, text))

    def test_a_boolean_is_not_accepted_as_a_number(self, tmp_path):
        """bool is a subclass of int in Python; accepting it would let
        `loss_pct = true` be read as 1%."""
        text = MINIMAL.replace("loss_pct = 10.0", "loss_pct = true")
        with pytest.raises(ConfigError, match="must be a number"):
            load_config(write(tmp_path, text))

    def test_negative_interval_is_rejected(self, tmp_path):
        with pytest.raises(ConfigError, match="interval_s must be positive"):
            load_config(write(tmp_path, "interval_s = -1\n" + MINIMAL))

    def test_zero_window_is_rejected(self, tmp_path):
        with pytest.raises(ConfigError, match="window_size must be at least 1"):
            load_config(write(tmp_path, "window_size = 0\n" + MINIMAL))

    def test_error_message_names_the_offending_target(self, tmp_path):
        text = FULL.replace("rtt_p95_ms = 250.0", "rtt_p95_ms = -5")
        with pytest.raises(ConfigError, match="'api'"):
            load_config(write(tmp_path, text))


class TestExampleConfigStaysValid:
    def test_shipped_example_parses(self):
        """The example in the README is the first thing anyone runs. If it
        stops parsing, the project is broken for every new user."""
        from pathlib import Path

        example = Path(__file__).parent.parent / "examples" / "netwatch.toml"
        cfg = load_config(example)
        assert cfg.targets


class TestUnknownKeys:
    """Silently dropping a typo'd key is the failure this validation exists to
    prevent: the monitor runs, looks healthy, and never fires."""

    def test_unknown_root_key_is_rejected(self, tmp_path):
        with pytest.raises(ConfigError, match="unknown key 'intervl_s'"):
            load_config(write(tmp_path, "intervl_s = 5.0\n" + MINIMAL))

    def test_unknown_target_key_is_rejected_and_names_the_target(self, tmp_path):
        text = MINIMAL + "los_pct = 5.0\n"
        with pytest.raises(ConfigError, match="gateway"):
            load_config(write(tmp_path, text))

    def test_several_unknown_keys_are_listed_together(self, tmp_path):
        with pytest.raises(ConfigError, match="unknown keys"):
            load_config(write(tmp_path, "foo = 1\nbar = 2\n" + MINIMAL))

    def test_the_message_lists_the_valid_keys(self, tmp_path):
        with pytest.raises(ConfigError, match="Valid keys:"):
            load_config(write(tmp_path, "nonsense = 1\n" + MINIMAL))

    def test_a_root_key_placed_after_a_target_is_caught(self, tmp_path):
        """TOML scoping puts it inside the target table. Before unknown-key
        rejection this was silently ignored and the history never appeared."""
        text = MINIMAL + 'history_path = "out/h.csv"\n'
        with pytest.raises(ConfigError, match="unknown key 'history_path'"):
            load_config(write(tmp_path, text))
