"""Config file support.

Everything configurable lives in one YAML file: config.yaml, next to
where you run the tool from (or wherever --config points).

    work_stations:
      - Sliedrecht
      - Ketelhaven
    max_gap_minutes: 20

If config.yaml doesn't exist, built-in defaults are used -- see
DEFAULT_WORK_STATIONS / DEFAULT_MAX_TRANSFER_GAP_MINUTES below. CLI flags
(--work-station, --max-gap) always override whatever's in the file.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_WORK_STATIONS = ["Sliedrecht", "Ketelhaven"]
DEFAULT_MAX_TRANSFER_GAP_MINUTES = 20

DEFAULT_CONFIG_PATH = Path("config.yaml")


@dataclass
class Config:
    work_stations: list[str]
    max_gap_minutes: int


def _resolve_config_path(path: Path | None = None) -> Path:
    if path is not None:
        return path

    cwd_candidate = Path("config.yaml")
    if cwd_candidate.is_file():
        return cwd_candidate

    package_candidate = Path(__file__).with_name("config.yaml")
    if package_candidate.is_file():
        return package_candidate

    return cwd_candidate


def load_config(path: Path | None = None) -> Config:
    """Loads config.yaml if it exists; falls back to built-in defaults if
    it doesn't. Raises ValueError (naming the bad field and the path) if
    the file exists but is malformed -- fail loudly rather than silently
    falling back, since that's confusing to debug."""
    resolved_path = _resolve_config_path(path)
    if not resolved_path.is_file():
        return Config(DEFAULT_WORK_STATIONS, DEFAULT_MAX_TRANSFER_GAP_MINUTES)

    try:
        data = yaml.safe_load(resolved_path.read_text()) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"{resolved_path}: invalid YAML ({e})") from e
    if not isinstance(data, dict):
        raise ValueError(f"{resolved_path}: expected a YAML mapping at the top level")

    work_stations = data.get("work_stations", DEFAULT_WORK_STATIONS)
    if not isinstance(work_stations, list) or not all(
        isinstance(s, str) for s in work_stations
    ):
        raise ValueError(f"{resolved_path}: 'work_stations' must be a list of strings")

    max_gap_minutes = data.get("max_gap_minutes", DEFAULT_MAX_TRANSFER_GAP_MINUTES)
    if not isinstance(max_gap_minutes, int) or isinstance(max_gap_minutes, bool):
        raise ValueError(f"{resolved_path}: 'max_gap_minutes' must be an integer")

    return Config(work_stations, max_gap_minutes)
