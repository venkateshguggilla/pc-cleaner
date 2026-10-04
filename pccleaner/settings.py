"""Persistent settings in %APPDATA%\\PCCleaner\\settings.json (shared by the GUI and the scheduler)."""
from __future__ import annotations

import json
import os

APP_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "PCCleaner")
SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")
LOG_DIR = os.path.join(APP_DIR, "logs")


def load() -> dict:
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def update(**values) -> None:
    """Merge `values` into the settings file (other keys are kept)."""
    data = load()
    data.update(values)
    os.makedirs(APP_DIR, exist_ok=True)
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, SETTINGS_FILE)
