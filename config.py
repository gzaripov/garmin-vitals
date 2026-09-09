# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Explicit local settings; demo mode never reads personal settings or data.

Optional settings.json keys: health_dir, data_dir, age, health_gain.
Environment overrides: HEALTH_DIR, VITALS_DATA_DIR, VITALS_AGE,
VITALS_HEALTH_GAIN. Relative paths are resolved from this project directory.
An explicit health_dir selects its recovery module and, unless overridden,
its data directory. Otherwise everything is standalone in ./data.
"""
import json
import math
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEMO = "--demo" in sys.argv


def _path(value):
    path = Path(value).expanduser()
    return path if path.is_absolute() else ROOT / path


def _number(value, name, default, minimum, maximum):
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    value = float(value)
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum:g} and {maximum:g}")
    return value


if DEMO:
    from demo import create_data

    HEALTH_DIR = None
    AGE = 34.0
    HEALTH_GAIN = 1.0
    DATA = create_data()
else:
    settings_path = ROOT / "settings.json"
    settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    if not isinstance(settings, dict):
        raise ValueError("settings.json must contain a JSON object")
    health_dir = os.environ.get("HEALTH_DIR", settings.get("health_dir"))
    data_dir = os.environ.get("VITALS_DATA_DIR", settings.get("data_dir"))
    HEALTH_DIR = _path(health_dir) if health_dir else None
    DATA = _path(data_dir) if data_dir else (HEALTH_DIR / "data" if HEALTH_DIR else ROOT / "data")
    AGE = _number(os.environ.get("VITALS_AGE", settings.get("age")), "age", None, 0, 130)
    HEALTH_GAIN = _number(os.environ.get("VITALS_HEALTH_GAIN", settings.get("health_gain")),
                          "health_gain", 1.0, 0, 10)
