"""Everything that differs between operating systems, in one place.

The rest of PC Cleaner does `from . import platform as plat` and calls `plat.<name>`; it never
imports winutil, regutil or the Task Scheduler code itself. The cleaner catalogs are the
exception: they are per-OS by nature (cleaners/__init__.py picks the right ones).

    NAME                                  "windows" or "linux"
    DAYS                                  weekday names for weekly schedules
    app_dir()                             folder for settings.json
    log_dir()                             folder for cleaning reports
    protected_paths()                     folders that must never be deleted (fsutil)
    is_admin() / relaunch_as_admin()
    running_processes()                   lower-case names of running programs
    close_processes(names, timeout=15.0)  ask programs to close; returns those still running
    kill_processes(names)                 force them closed; returns those still running
    send_to_trash(paths, window=None)     returns the paths that could not be moved
    show_in_file_manager(path) / open_folder(path)
    set_dark_title_bar(window, dark)      match the window frame to the theme (no-op if n/a)
    install_shortcuts(desktop, remove)    app-menu / desktop entries; returns an exit code
    schedule_install(frequency, time_of_day, day)  returns True if it runs with admin rights
    schedule_remove() / schedule_installed() / schedule_next_run()

Windows: windows.py (the 1.x behaviour). Linux: linux.py.
"""
from __future__ import annotations

import sys

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

if sys.platform == "win32":
    from .windows import *  # noqa: E402,F401,F403
else:
    from .linux import *  # noqa: E402,F401,F403
