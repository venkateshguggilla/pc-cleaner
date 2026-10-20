"""Windows implementation of pccleaner.platform: the same behaviour as PC Cleaner 1.x."""
from __future__ import annotations

import ctypes
import datetime
import os
import subprocess
import sys
import tempfile
from xml.sax.saxutils import escape

from .. import winutil
from . import DAYS

__all__ = [
    "NAME", "app_dir", "log_dir", "protected_paths", "is_admin", "relaunch_as_admin", "running_processes",
    "close_processes", "kill_processes", "send_to_trash", "show_in_file_manager", "open_folder",
    "set_dark_title_bar", "install_shortcuts", "schedule_install", "schedule_remove",
    "schedule_installed", "schedule_next_run",
]

NAME = "windows"
TASK_NAME = "PCCleaner"


def app_dir() -> str:
    return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "PCCleaner")


def log_dir() -> str:
    return os.path.join(app_dir(), "logs")


def protected_paths() -> list[str]:
    env = os.environ
    drive = env.get("SystemDrive", "C:") + "\\"
    windir = env.get("SystemRoot", drive + "Windows")
    profile = env.get("USERPROFILE", "")
    paths = [
        drive, windir, os.path.join(windir, "System32"), os.path.join(drive, "Users"),
        env.get("ProgramFiles", ""), env.get("ProgramFiles(x86)", ""), env.get("ProgramW6432", ""),
        env.get("ProgramData", ""), env.get("PUBLIC", ""), env.get("APPDATA", ""),
        env.get("LOCALAPPDATA", ""), env.get("TEMP", ""), env.get("TMP", ""), profile,
    ]
    if profile:
        paths += [os.path.join(profile, d) for d in
                  ("Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos", "OneDrive")]
    return [p for p in paths if p]


# ---- rights and programs ------------------------------------------------------------------

is_admin = winutil.is_admin
relaunch_as_admin = winutil.relaunch_as_admin
running_processes = winutil.running_processes
close_processes = winutil.close_processes
kill_processes = winutil.kill_processes


# ---- files and windows --------------------------------------------------------------------

def send_to_trash(paths: list[str], window=None) -> list[str]:
    return winutil.send_to_recycle_bin(paths, hwnd=window.winfo_id() if window is not None else None)


def show_in_file_manager(path: str) -> None:
    winutil.show_in_explorer(path)


def open_folder(path: str) -> None:
    os.startfile(path)


def set_dark_title_bar(window, dark: bool) -> None:
    hwnd = ctypes.windll.user32.GetParent(window.winfo_id()) or window.winfo_id()
    winutil.set_dark_title_bar(hwnd, dark)


# ---- Start-menu / desktop shortcuts ---------------------------------------------------------

def shortcut_paths(desktop: bool) -> list[str]:
    start_menu = os.path.join(os.environ["APPDATA"], r"Microsoft\Windows\Start Menu\Programs")
    paths = [os.path.join(start_menu, "PC Cleaner.lnk")]
    if desktop:
        folder = winutil.known_folder("B4BFCC3A-DB2C-424C-B029-7FE99A87C641") or \
            os.path.join(os.environ["USERPROFILE"], "Desktop")
        paths.append(os.path.join(folder, "PC Cleaner.lnk"))
    return paths


def install_shortcuts(desktop: bool = False, remove: bool = False) -> int:
    """Shortcuts that start the app through Python's own signed pythonw.exe, so it runs on
    PCs where Smart App Control blocks the unsigned PCCleaner.exe."""
    if getattr(sys, "frozen", False):
        print("This is the packaged exe - use the installer for shortcuts.")
        return 1
    paths = shortcut_paths(desktop=desktop or remove)
    if remove:
        for path in paths:
            if os.path.exists(path):
                os.remove(path)
                print(f"Removed {path}")
        return 0
    project = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pythonw):
        print(f"pythonw.exe not found next to {sys.executable}")
        return 1
    for path in paths:
        winutil.create_shortcut(path, pythonw, "-m pccleaner", project,
                                os.path.join(project, "assets", "pccleaner.ico"), "PC Cleaner")
        print(f"Created {path}")
    print("Open PC Cleaner from the Start menu.")
    return 0


# ---- scheduled cleaning: Windows Task Scheduler -----------------------------------------------
# The task runs `pythonw -m pccleaner scheduled-run` (or `PCCleaner.exe scheduled-run`).

def _command() -> tuple[str, str, str]:
    """(program, arguments, working directory) for the scheduled task."""
    if getattr(sys, "frozen", False):
        return sys.executable, "scheduled-run", os.path.dirname(sys.executable)
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    exe = pythonw if os.path.exists(pythonw) else sys.executable
    project = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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


def schedule_install(frequency: str, time_of_day: str, day: str) -> bool:
    """Create or replace the task. Returns True if it runs with administrator rights."""
    elevated = is_admin()
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
    return elevated


def schedule_remove() -> None:
    result = _schtasks("/Delete", "/F", "/TN", TASK_NAME)
    if result.returncode and schedule_installed():
        raise RuntimeError((result.stderr or result.stdout).strip())


def schedule_installed() -> bool:
    return _schtasks("/Query", "/TN", TASK_NAME).returncode == 0


def schedule_next_run() -> str | None:
    """Next run time as text, or None if the task does not exist."""
    if not schedule_installed():
        return None
    result = winutil.run_powershell(
        f"$i = Get-ScheduledTaskInfo -TaskName '{TASK_NAME}'; "
        "if ($i.NextRunTime) { $i.NextRunTime.ToString('ddd dd MMM yyyy HH:mm') }", timeout=60)
    return result.stdout.strip() or "unknown"
