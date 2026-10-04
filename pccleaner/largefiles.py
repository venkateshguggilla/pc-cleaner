"""Large file finder: walk a folder and report the biggest files (read-only)."""
from __future__ import annotations

import heapq
import os
import stat
import threading
import time
from dataclasses import dataclass
from typing import Callable

MAX_RESULTS = 2000
_SYSTEM = getattr(stat, "FILE_ATTRIBUTE_SYSTEM", 0x4)
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


@dataclass(order=True)
class FileInfo:
    size: int
    path: str
    modified: float


def system_folders() -> set[str]:
    """Folders skipped when 'skip Windows and program folders' is on."""
    env = os.environ
    drive = env.get("SystemDrive", "C:") + "\\"
    paths = [env.get("SystemRoot", drive + "Windows"), env.get("ProgramFiles", ""), env.get("ProgramFiles(x86)", ""),
             env.get("ProgramW6432", ""), env.get("ProgramData", ""),
             os.path.join(drive, "$Recycle.Bin"), os.path.join(drive, "System Volume Information"),
             os.path.join(drive, "Recovery"), os.path.join(drive, "$WinREAgent")]
    return {os.path.normcase(os.path.abspath(p)) for p in paths if p}


def is_app_file(path: str) -> bool:
    """True for files that belong to installed programs rather than to you: anything in
    AppData / ProgramData / Program Files, or in hidden dot-folders like .android or .codex."""
    env = os.environ
    p = os.path.normcase(os.path.abspath(path))
    local = env.get("LOCALAPPDATA", "")
    roots = [env.get("APPDATA", ""), local, os.path.join(os.path.dirname(local), "LocalLow") if local else "",
             env.get("ProgramData", ""), env.get("ProgramFiles", ""), env.get("ProgramFiles(x86)", ""),
             env.get("SystemRoot", "")]
    if any(r and p.startswith(os.path.normcase(r) + os.sep) for r in roots):
        return True
    profile = os.path.normcase(env.get("USERPROFILE", ""))
    if profile and p.startswith(profile + os.sep):
        return any(part.startswith(".") for part in p[len(profile) + 1:].split(os.sep)[:-1])
    return False


def find(root: str, min_bytes: int, min_age_days: float = 0, skip_system: bool = True,
         cancel: threading.Event | None = None,
         progress: Callable[[int, int], None] | None = None) -> list[FileInfo]:
    """Largest files under `root` (at least `min_bytes`, not modified for `min_age_days`),
    biggest first, at most MAX_RESULTS. Links/junctions are not followed."""
    cutoff = time.time() - min_age_days * 86400 if min_age_days else None
    skipped = system_folders() if skip_system else set()
    heap: list[FileInfo] = []
    stack = [root]
    folders = matched = 0
    last_report = 0.0

    while stack:
        if cancel is not None and cancel.is_set():
            break
        folder = stack.pop()
        folders += 1
        try:
            with os.scandir(folder) as it:
                entries = list(it)
        except OSError:
            continue
        for e in entries:
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            attrs = getattr(st, "st_file_attributes", 0)
            if attrs & _REPARSE:
                continue
            if stat.S_ISDIR(st.st_mode):
                if os.path.normcase(e.path) not in skipped:
                    stack.append(e.path)
            elif st.st_size >= min_bytes and not (skip_system and attrs & _SYSTEM):
                if cutoff is None or st.st_mtime <= cutoff:
                    matched += 1
                    item = FileInfo(st.st_size, e.path, st.st_mtime)
                    if len(heap) < MAX_RESULTS:
                        heapq.heappush(heap, item)
                    else:
                        heapq.heappushpop(heap, item)
        if progress and time.monotonic() - last_report > 0.2:
            last_report = time.monotonic()
            progress(folders, matched)

    if progress:
        progress(folders, matched)
    return sorted(heap, reverse=True)
