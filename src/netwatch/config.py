"""Load and validate configuration from TOML.

Validation is strict and happens once, at load. A monitor that starts with a
typo in a threshold and discovers it three hours later, when the alert that
should have fired did not, is worse than one that refuses to start.

Every error message names the target it came from, because "invalid value" in
a file with twenty targets is not a diagnosis.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from netwatch.alerting import Thresholds

__all__ = ["Config", "ConfigError", "TargetConfig", "load_config"]

VALID_KINDS = frozenset({"icmp", "tcp"})

# Unknown keys are an error, not something to ignore. A monitor that silently
# drops `los_pct = 5` because of a typo is worse than one that will not start:
# it runs, looks healthy, and never fires the alert it was configured for.
TARGET_KEYS = frozenset(
    {
        "name",
        "kind",
        "host",
        "port",
        "timeout_s",
        "rtt_p95_ms",
        "loss_pct",
        "for_breaches",
        "clear_after",
    }
)
ROOT_KEYS = frozenset({"interval_s", "window_size", "history_path", "target"})


def _reject_unknown(keys: object, allowed: frozenset[str], where: str) -> None:
    if not isinstance(keys, dict):
        return
    unknown = sorted(set(keys) - allowed)
    if unknown:
        raise ConfigError(
            f"{where}: unknown key{'s' if len(unknown) > 1 else ''} "
            f"{', '.join(repr(k) for k in unknown)}. Valid keys: {', '.join(sorted(allowed))}"
        )


class ConfigError(ValueError):
    """Raised for any problem with the configuration file."""


@dataclass(frozen=True, slots=True)
class TargetConfig:
    name: str
    kind: str
    host: str
    thresholds: Thresholds
    port: int | None = None
    timeout_s: float = 2.0


@dataclass(frozen=True, slots=True)
class Config:
    interval_s: float
    window_size: int
    targets: tuple[TargetConfig, ...]
    history_path: Path | None = None

    def thresholds_by_target(self) -> dict[str, Thresholds]:
        return {t.name: t.thresholds for t in self.targets}


def _require(mapping: dict[str, Any], key: str, where: str) -> Any:
    if key not in mapping:
        raise ConfigError(f"{where}: missing required key {key!r}")
    return mapping[key]


def _as_float(value: Any, key: str, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where}: {key} must be a number, got {type(value).__name__}")
    return float(value)


def _as_int(value: Any, key: str, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where}: {key} must be an integer, got {type(value).__name__}")
    return value


def _parse_target(raw: dict[str, Any], index: int) -> TargetConfig:
    where = f"target[{index}]"
    name = raw.get("name")
    if isinstance(name, str) and name.strip():
        where = f"target {name!r}"
    _reject_unknown(raw, TARGET_KEYS, where)

    name = _require(raw, "name", where)
    if not isinstance(name, str) or not name.strip():
        raise ConfigError(f"{where}: name must be a non-empty string")
    where = f"target {name!r}"

    kind = _require(raw, "kind", where)
    if kind not in VALID_KINDS:
        raise ConfigError(f"{where}: kind must be one of {sorted(VALID_KINDS)}, got {kind!r}")

    host = _require(raw, "host", where)
    if not isinstance(host, str) or not host.strip():
        raise ConfigError(f"{where}: host must be a non-empty string")

    port: int | None = None
    if kind == "tcp":
        port = _as_int(_require(raw, "port", where), "port", where)
        if not 1 <= port <= 65535:
            raise ConfigError(f"{where}: port must be between 1 and 65535, got {port}")
    elif "port" in raw:
        raise ConfigError(f"{where}: an icmp target must not specify a port")

    timeout_s = _as_float(raw.get("timeout_s", 2.0), "timeout_s", where)
    if timeout_s <= 0:
        raise ConfigError(f"{where}: timeout_s must be positive")

    try:
        thresholds = Thresholds(
            rtt_p95_ms=(
                _as_float(raw["rtt_p95_ms"], "rtt_p95_ms", where) if "rtt_p95_ms" in raw else None
            ),
            loss_pct=(_as_float(raw["loss_pct"], "loss_pct", where) if "loss_pct" in raw else None),
            for_breaches=_as_int(raw.get("for_breaches", 3), "for_breaches", where),
            clear_after=_as_int(raw.get("clear_after", 5), "clear_after", where),
        )
    except ValueError as exc:
        # Thresholds does its own invariant checking; re-raise with the target
        # attached so the message is actionable.
        raise ConfigError(f"{where}: {exc}") from exc

    return TargetConfig(
        name=name,
        kind=kind,
        host=host,
        port=port,
        timeout_s=timeout_s,
        thresholds=thresholds,
    )


def load_config(path: Path) -> Config:
    """Parse and validate a configuration file.

    Raises:
        ConfigError: on a missing file, malformed TOML, or any invalid value.
    """
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc

    try:
        data = tomllib.loads(raw_bytes.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc

    _reject_unknown(data, ROOT_KEYS, str(path))

    interval_s = _as_float(data.get("interval_s", 10.0), "interval_s", str(path))
    if interval_s <= 0:
        raise ConfigError(f"{path}: interval_s must be positive")

    window_size = _as_int(data.get("window_size", 10), "window_size", str(path))
    if window_size < 1:
        raise ConfigError(f"{path}: window_size must be at least 1")

    raw_targets = data.get("target", [])
    if not isinstance(raw_targets, list) or not raw_targets:
        raise ConfigError(f"{path}: at least one [[target]] is required")

    targets = tuple(_parse_target(t, i) for i, t in enumerate(raw_targets))

    seen: set[str] = set()
    for t in targets:
        if t.name in seen:
            raise ConfigError(f"{path}: duplicate target name {t.name!r}")
        seen.add(t.name)

    history = data.get("history_path")
    if history is not None and not isinstance(history, str):
        raise ConfigError(f"{path}: history_path must be a string")

    return Config(
        interval_s=interval_s,
        window_size=window_size,
        targets=targets,
        history_path=Path(history) if history else None,
    )
