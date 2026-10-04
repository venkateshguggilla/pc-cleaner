"""Scheduled cleaning through Windows Task Scheduler.

The task runs `pythonw -m pccleaner scheduled-run` (or `PCCleaner.exe scheduled-run`),
which cleans the items saved in settings["schedule"]["items"] without any prompts.
"""
from __future__ import annotations

import datetime
import os
import subprocess
import sys
import tempfile
from xml.sax.saxutils import escape

from . import report, settings, winutil
from .stats import Stats

TASK_NAME = "PCCleaner"
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _command() -> tuple[str, str, str]:
    """(program, arguments, working directory) for the scheduled task."""
    if getattr(sys, "frozen", False):
        return sys.executable, "scheduled-run", os.path.dirname(sys.executable)
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    exe = pythonw if os.path.exists(pythonw) else sys.executable
    project = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return exe, "-m pccleaner scheduled-run", project


def task_xml(frequency: str, time_of_day: str, day: str = "Sunday", elevated: bool = False) -> str:
    hour, minute = (int(x) for x in time_of_day.split(":"))
    start = datetime.datetime.combine(datetime.date.today(), datetime.time(hour, minute))
    if frequency == "daily":
        schedule = "<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>"
    elif frequency == "weekly":
        if day not in DAYS:
            raise ValueError(f"Unknown day: {day}")
        schedule = (f"<ScheduleByWeek><DaysOfWeek><{day} /></DaysOfWeek>"
                    "<WeeksInterval>1</WeeksInterval></ScheduleByWeek>")
    else:
        raise ValueError(f"Unknown frequency: {frequency}")
    program, args, workdir = _command()
    user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".lstrip("\\")
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>PC Cleaner scheduled cleaning</Description></RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{start:%Y-%m-%dT%H:%M:%S}</StartBoundary>
      <Enabled>true</Enabled>
      {schedule}
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>{"HighestAvailable" if elevated else "LeastPrivilege"}</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT1H</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(program)}</Command>
      <Arguments>{escape(args)}</Arguments>
      <WorkingDirectory>{escape(workdir)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _schtasks(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["schtasks", *args], capture_output=True, text=True, errors="replace",
                          creationflags=subprocess.CREATE_NO_WINDOW, timeout=60)


def install(frequency: str, time_of_day: str, day: str, items: list[str]) -> None:
    """Create or replace the task and save which items it cleans."""
    elevated = winutil.is_admin()
    xml = task_xml(frequency, time_of_day, day, elevated)
    fd, path = tempfile.mkstemp(suffix=".xml")
    try:
        with os.fdopen(fd, "w", encoding="utf-16") as f:
            f.write(xml)
        result = _schtasks("/Create", "/F", "/TN", TASK_NAME, "/XML", path)
    finally:
        os.remove(path)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip() or "schtasks failed")
    settings.update(schedule={"frequency": frequency, "time": time_of_day, "day": day,
                              "items": items, "elevated": elevated})


def remove() -> None:
    result = _schtasks("/Delete", "/F", "/TN", TASK_NAME)
    if result.returncode and is_installed():
        raise RuntimeError((result.stderr or result.stdout).strip())
    data = settings.load().get("schedule")
    if data:
        settings.update(schedule=None)


def is_installed() -> bool:
    return _schtasks("/Query", "/TN", TASK_NAME).returncode == 0


def next_run() -> str | None:
    """Next run time as text, or None if the task does not exist."""
    if not is_installed():
        return None
    result = winutil.run_powershell(
        f"$i = Get-ScheduledTaskInfo -TaskName '{TASK_NAME}'; "
        "if ($i.NextRunTime) { $i.NextRunTime.ToString('ddd dd MMM yyyy HH:mm') }", timeout=60)
    return result.stdout.strip() or "unknown"


def describe(schedule: dict) -> str:
    when = "Every day" if schedule.get("frequency") == "daily" else f"Every {schedule.get('day')}"
    return f"{when} at {schedule.get('time')}"


def run_scheduled() -> str:
    """Clean the scheduled items unattended. Returns the report path."""
    from .cleaners import build_catalog

    schedule = settings.load().get("schedule") or {}
    wanted = schedule.get("items", [])
    by_id = {t.id: t for t in build_catalog()}
    admin = winutil.is_admin()
    running = winutil.running_processes()
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
