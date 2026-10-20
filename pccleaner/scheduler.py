"""Scheduled cleaning.

The system scheduler (Windows Task Scheduler, or a systemd user timer on Linux; see
platform) runs `pccleaner scheduled-run`, which cleans the items saved in
settings["schedule"]["items"] without any prompts.
"""
from __future__ import annotations

from . import platform as plat
from . import report, settings
from .stats import Stats

DAYS = plat.DAYS


def install(frequency: str, time_of_day: str, day: str, items: list[str]) -> None:
    """Create or replace the schedule and save which items it cleans."""
    elevated = plat.schedule_install(frequency, time_of_day, day)
    settings.update(schedule={"frequency": frequency, "time": time_of_day, "day": day,
                              "items": items, "elevated": elevated})


def remove() -> None:
    plat.schedule_remove()
    data = settings.load().get("schedule")
    if data:
        settings.update(schedule=None)


def is_installed() -> bool:
    return plat.schedule_installed()


def next_run() -> str | None:
    """Next run time as text, or None if there is no schedule."""
    return plat.schedule_next_run()


def describe(schedule: dict) -> str:
    when = "Every day" if schedule.get("frequency") == "daily" else f"Every {schedule.get('day')}"
    return f"{when} at {schedule.get('time')}"


def run_scheduled() -> str:
    """Clean the scheduled items unattended. Returns the report path."""
    from .cleaners import build_catalog

    schedule = settings.load().get("schedule") or {}
    wanted = schedule.get("items", [])
    by_id = {t.id: t for t in build_catalog()}
    admin = plat.is_admin()
    running = plat.running_processes()
    rows: list[tuple[str, Stats]] = []
    extra = []
    for item_id in wanted:
        t = by_id.get(item_id)
        if t is None:
            extra.append(f"Skipped {item_id}: no longer available on this PC")
            continue
        label = f"{t.group} / {t.name}"
        if t.admin and not admin:
            extra.append(f"Skipped {label}: needs administrator rights")
            continue
        open_apps = [p for p in t.processes if p in running]
        if open_apps:
            extra.append(f"Skipped {label}: {', '.join(open_apps)} was running")
            continue
        try:
            rows.append((label, t.clean()))
        except Exception as exc:
            failed = Stats()
            failed.note(f"Failed: {exc}")
            rows.append((label, failed))
    if extra:
        extra = [""] + extra
    return report.write("Scheduled cleaning", rows, extra)
