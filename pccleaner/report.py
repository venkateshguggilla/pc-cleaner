"""Plain-text reports of each cleaning run, in the logs folder next to settings.json."""
from __future__ import annotations

import datetime
import glob
import os

from .settings import LOG_DIR
from .stats import Stats, human_size

KEEP_LOGS = 50


def write(title: str, rows: list[tuple[str, Stats]], extra: list[str] = ()) -> str:
    """rows: (item label, stats). Returns the log file path."""
    now = datetime.datetime.now()
    total = Stats()
    lines = [f"{title} - {now:%Y-%m-%d %H:%M:%S}", ""]
    for label, stats in rows:
        total.merge(stats)
        found = f"{stats.files:,} files, {stats.entries:,} entries, {human_size(stats.bytes)}"
        notes = "; ".join(stats.messages + ([f"{stats.skipped:,} in use/denied"] if stats.skipped else []))
        lines.append(f"{label:60} {found}" + (f"   [{notes}]" if notes else ""))
    lines += ["", f"Total: {human_size(total.bytes)} freed, {total.files:,} files, {total.entries:,} entries"]
    lines += list(extra)

    os.makedirs(LOG_DIR, exist_ok=True)
    path = os.path.join(LOG_DIR, f"{now:%Y-%m-%d_%H%M%S}.log")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    for old in sorted(glob.glob(os.path.join(LOG_DIR, "*.log")))[:-KEEP_LOGS]:
        try:
            os.remove(old)
        except OSError:
            pass
    return path
