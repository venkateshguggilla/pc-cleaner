"""Linux implementation of pccleaner.platform (RudramOS and other systemd desktops).

- Settings in ~/.config/pccleaner, reports in ~/.local/state/pccleaner/logs (XDG).
- Programs are found in /proc and closed with SIGTERM, then SIGKILL if asked; only your own.
- Deleting in Large files moves to the Trash (gio trash); "show" opens the file manager.
- The app never runs as root: admin items ask for approval when they're cleaned (pkexec helper).
- Scheduled cleaning is a systemd user timer with Persistent=true, so a run missed while the PC
  was off happens at the next start.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from urllib.parse import quote

from . import DAYS

__all__ = [
    "NAME", "app_dir", "log_dir", "protected_paths", "is_admin", "relaunch_as_admin",
    "running_processes", "close_processes", "kill_processes", "send_to_trash", "show_in_file_manager",
    "open_folder", "set_dark_title_bar", "install_shortcuts", "schedule_install", "schedule_remove",
    "schedule_installed", "schedule_next_run",
]

NAME = "linux"
UNIT = "pccleaner"
SYSTEM_LAUNCHER = "/usr/bin/pc-cleaner"  # RudramOS installs PC Cleaner here


def _xdg(variable: str, fallback: str) -> str:
    return os.environ.get(variable) or os.path.join(os.path.expanduser("~"), fallback)


def app_dir() -> str:
    return os.path.join(_xdg("XDG_CONFIG_HOME", ".config"), "pccleaner")


def log_dir() -> str:
    return os.path.join(_xdg("XDG_STATE_HOME", ".local/state"), "pccleaner", "logs")


def protected_paths() -> list[str]:
    home = os.path.expanduser("~")
    system = ["/", "/bin", "/boot", "/dev", "/efi", "/etc", "/home", "/lib", "/lib64", "/media",
              "/mnt", "/opt", "/proc", "/root", "/run", "/sbin", "/srv", "/sys", "/sysroot", "/tmp",
              "/usr", "/var", "/var/home", "/var/lib", "/var/log"]
    user = [home] + [os.path.join(home, d) for d in (
        "Desktop", "Documents", "Downloads", "Music", "Pictures", "Public", "Templates", "Videos",
        ".cache", ".config", ".local", ".local/share", ".local/state", ".var", ".var/app", ".ssh",
        ".gnupg", ".mozilla", ".pki")]
    return system + user


# ---- rights and programs ------------------------------------------------------------------

def is_admin() -> bool:
    return os.geteuid() == 0


def relaunch_as_admin() -> bool:
    return False  # by design: admin items ask for approval one at a time instead


def _my_processes():
    """(pid, lower-case names) of this user's programs: the kernel name and argv[0]'s file name."""
    me = os.getuid()
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            if os.stat(f"/proc/{entry}").st_uid != me:
                continue
            with open(f"/proc/{entry}/comm", encoding="utf-8", errors="replace") as f:
                names = {f.read().strip().lower()}
            with open(f"/proc/{entry}/cmdline", "rb") as f:
                argv0 = f.read().split(b"\0", 1)[0].decode("utf-8", "replace")
            if argv0:
                names.add(os.path.basename(argv0).lower())
        except OSError:
            continue  # it ended meanwhile
        yield int(entry), names


def running_processes() -> set[str]:
    return {name for _pid, names in _my_processes() for name in names}


def _pids(names) -> dict[int, str]:
    wanted = {n.lower() for n in names}
    found = {}
    for pid, have in _my_processes():
        match = wanted & have
        if match and pid != os.getpid():
            found[pid] = match.pop()
    return found


def _signal_and_wait(names, sig, timeout: float) -> set[str]:
    targets = _pids(names)
    for pid in targets:
        try:
            os.kill(pid, sig)
        except OSError:
            pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pids(names):
            return set()
        time.sleep(0.25)
    return set(_pids(names).values())


def close_processes(names, timeout: float = 15.0) -> set[str]:
    """Ask the programs to quit (SIGTERM: they save and close normally). Returns those still running."""
    return _signal_and_wait(names, signal.SIGTERM, timeout)


def kill_processes(names) -> set[str]:
    """Force them closed (SIGKILL). Returns those still running."""
    return _signal_and_wait(names, signal.SIGKILL, 3.0)


# ---- files and windows --------------------------------------------------------------------

def _run(argv, timeout=60) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def send_to_trash(paths: list[str], window=None) -> list[str]:
    """Moves files to the Trash. Returns the paths that could not be moved."""
    failed = []
    for path in paths:
        result = _run(["gio", "trash", "--", path])
        if result is None or result.returncode != 0:
            failed.append(path)
    return failed


def show_in_file_manager(path: str) -> None:
    uri = "file://" + quote(os.path.abspath(path))
    result = _run(["gdbus", "call", "--session", "--dest", "org.freedesktop.FileManager1",
                   "--object-path", "/org/freedesktop/FileManager1",
                   "--method", "org.freedesktop.FileManager1.ShowItems", f"['{uri}']", ""], timeout=10)
    if result is None or result.returncode != 0:
        open_folder(os.path.dirname(path) or path)


def open_folder(path: str) -> None:
    try:
        subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def set_dark_title_bar(window, dark: bool) -> None:
    pass  # the desktop draws the window frame and follows its own theme


# ---- app-menu entry -------------------------------------------------------------------------

def _project() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def desktop_entry() -> str:
    project = _project()
    return ("[Desktop Entry]\nType=Application\nName=PC Cleaner\n"
            "Comment=Remove junk files and privacy traces\n"
            f"Exec={sys.executable} -m pccleaner\nPath={project}\n"
            f"Icon={os.path.join(project, 'assets', 'pccleaner.png')}\n"
            "Terminal=false\nCategories=Utility;System;\nKeywords=clean;junk;cache;privacy;history;\n")


def install_shortcuts(desktop: bool = False, remove: bool = False) -> int:
    """App-menu entry for a copy run from source (RudramOS ships its own entry)."""
    if _project().startswith("/usr/"):
        print("PC Cleaner is installed with the system; it's already in the app menu.")
        return 0
    path = os.path.join(_xdg("XDG_DATA_HOME", ".local/share"), "applications", "pc-cleaner.desktop")
    if remove:
        if os.path.exists(path):
            os.remove(path)
            print(f"Removed {path}")
        return 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(desktop_entry())
    print(f"Created {path}. Open PC Cleaner from the app menu.")
    return 0


# ---- scheduled cleaning: systemd user timer ----------------------------------------------------

def _unit_dir() -> str:
    return os.path.join(_xdg("XDG_CONFIG_HOME", ".config"), "systemd", "user")


def unit_files(frequency: str, time_of_day: str, day: str = "Sunday") -> tuple[str, str]:
    """(pccleaner.service, pccleaner.timer) contents."""
    hour, minute = (int(x) for x in time_of_day.split(":"))
    if frequency == "daily":
        calendar = f"*-*-* {hour:02d}:{minute:02d}:00"
    elif frequency == "weekly":
        if day not in DAYS:
            raise ValueError(f"Unknown day: {day}")
        calendar = f"{day[:3]} *-*-* {hour:02d}:{minute:02d}:00"
    else:
        raise ValueError(f"Unknown frequency: {frequency}")
    if os.path.exists(SYSTEM_LAUNCHER) and _project().startswith("/usr/"):
        exec_line, workdir = f"{SYSTEM_LAUNCHER} scheduled-run", ""
    else:
        exec_line, workdir = f"{sys.executable} -m pccleaner scheduled-run", f"WorkingDirectory={_project()}\n"
    service = ("[Unit]\nDescription=PC Cleaner scheduled cleaning\n\n"
               f"[Service]\nType=oneshot\nExecStart={exec_line}\n{workdir}"
               "Nice=10\nIOSchedulingClass=idle\n")
    timer = ("[Unit]\nDescription=PC Cleaner scheduled cleaning\n\n"
             f"[Timer]\nOnCalendar={calendar}\nPersistent=true\n\n"
             "[Install]\nWantedBy=timers.target\n")
    return service, timer


def _systemctl(*args: str) -> subprocess.CompletedProcess | None:
    return _run(["systemctl", "--user", *args], timeout=30)


def schedule_install(frequency: str, time_of_day: str, day: str) -> bool:
    service, timer = unit_files(frequency, time_of_day, day)
    os.makedirs(_unit_dir(), exist_ok=True)
    for name, text in ((f"{UNIT}.service", service), (f"{UNIT}.timer", timer)):
        with open(os.path.join(_unit_dir(), name), "w", encoding="utf-8") as f:
            f.write(text)
    _systemctl("daemon-reload")
    result = _systemctl("enable", "--now", f"{UNIT}.timer")
    if result is None or result.returncode != 0:
        raise RuntimeError((result.stderr if result else "") or "Could not start the systemd timer")
    _systemctl("restart", f"{UNIT}.timer")  # pick up a changed time
    return False  # runs as you; admin items are skipped


def schedule_remove() -> None:
    _systemctl("disable", "--now", f"{UNIT}.timer")
    for name in (f"{UNIT}.service", f"{UNIT}.timer"):
        try:
            os.remove(os.path.join(_unit_dir(), name))
        except FileNotFoundError:
            pass
    _systemctl("daemon-reload")


def schedule_installed() -> bool:
    result = _systemctl("is-enabled", f"{UNIT}.timer")
    return bool(result) and result.stdout.strip() == "enabled"


def schedule_next_run() -> str | None:
    if not schedule_installed():
        return None
    result = _systemctl("show", f"{UNIT}.timer", "-p", "NextElapseUSecRealtime", "--value")
    value = result.stdout.strip() if result else ""
    return value if value and value != "n/a" else "unknown"
