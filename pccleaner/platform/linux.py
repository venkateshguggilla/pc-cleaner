"""Linux implementation of pccleaner.platform (RudramOS port).

Phase 10a only moves the Windows code behind the platform interface; the Linux functions are
filled in by Phase 10b (see docs in the RudramOS repo, DESIGN §9.2). Until then importing works
(so the package and its tests load on Linux), and using a function says it isn't ready yet.
"""
from __future__ import annotations

import os

__all__ = [
    "NAME", "app_dir", "protected_paths", "is_admin", "relaunch_as_admin", "running_processes",
    "close_processes", "kill_processes", "send_to_trash", "show_in_file_manager", "open_folder",
    "set_dark_title_bar", "install_shortcuts", "schedule_install", "schedule_remove",
    "schedule_installed", "schedule_next_run",
]

NAME = "linux"


def _todo(*_args, **_kwargs):
    raise NotImplementedError("PC Cleaner for Linux is not finished yet (RudramOS Phase 10b)")


def app_dir() -> str:
    return os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "pccleaner")


def protected_paths() -> list[str]:
    return ["/", "/usr", "/etc", "/boot", "/proc", "/sys", os.path.expanduser("~")]


def is_admin() -> bool:
    return os.geteuid() == 0


def set_dark_title_bar(window, dark: bool) -> None:
    pass  # the desktop draws the frame and follows its own theme


relaunch_as_admin = running_processes = close_processes = kill_processes = _todo
send_to_trash = show_in_file_manager = open_folder = install_shortcuts = _todo
schedule_install = schedule_remove = schedule_installed = schedule_next_run = _todo
