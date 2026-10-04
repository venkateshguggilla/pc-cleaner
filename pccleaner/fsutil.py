"""Filesystem helpers: measure and delete paths without ever following links.

Safety rules:
  * symlinks / junctions are removed as links, never traversed;
  * paths that are (or contain) important system or user folders are refused;
  * files modified after `cutoff` (a timestamp) are left alone.
"""
from __future__ import annotations

import glob
import os
import stat
from functools import lru_cache

from .stats import Stats

_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_READONLY = getattr(stat, "FILE_ATTRIBUTE_READONLY", 0x1)


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path)).rstrip("\\/")


@lru_cache(maxsize=1)
def _protected() -> frozenset[str]:
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
    return frozenset(_norm(p) for p in paths if p)


def is_safe(path: str) -> bool:
    """False for drive roots, shallow paths and protected folders or their ancestors."""
    n = _norm(path)
    if n.count(os.sep) < 2:
        return False
    return not any(n == p or p.startswith(n + os.sep) for p in _protected())


def _is_link(st: os.stat_result) -> bool:
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & _REPARSE_POINT)


def measure(path: str, stats: Stats, cutoff: float | None = None) -> None:
    """Add the files under `path` that `remove` would delete to `stats`."""
    try:
        st = os.lstat(path)
    except OSError:
        return
    if _is_link(st):
        stats.files += 1
    elif stat.S_ISDIR(st.st_mode):
        try:
            with os.scandir(path) as it:
                entries = [e.path for e in it]
        except OSError:
            return
        for entry in entries:
            measure(entry, stats, cutoff)
    elif cutoff is None or st.st_mtime <= cutoff:
        stats.files += 1
        stats.bytes += st.st_size


def remove(path: str, stats: Stats, cutoff: float | None = None) -> None:
    """Delete a file or a whole directory tree, skipping (and counting) files in use."""
    if not is_safe(path):
        stats.note(f"Refused protected path: {path}")
        return
    _remove(path, stats, cutoff)


def _remove(path: str, stats: Stats, cutoff: float | None) -> None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError:
        stats.skipped += 1
        return

    if _is_link(st):
        try:
            os.rmdir(path) if stat.S_ISDIR(st.st_mode) else os.unlink(path)
            stats.files += 1
        except OSError:
            stats.skipped += 1
    elif stat.S_ISDIR(st.st_mode):
        try:
            with os.scandir(path) as it:
                entries = [e.path for e in it]
        except OSError:
            stats.skipped += 1
            return
        for entry in entries:
            _remove(entry, stats, cutoff)
        try:
            os.rmdir(path)
        except OSError:
            pass  # still holds files that are in use or too new
    elif cutoff is None or st.st_mtime <= cutoff:
        try:
            if getattr(st, "st_file_attributes", 0) & _READONLY:
                os.chmod(path, stat.S_IWRITE)
            os.unlink(path)
        except FileNotFoundError:
            return
        except OSError:
            stats.skipped += 1
            return
        stats.files += 1
        stats.bytes += st.st_size


# ---- path selectors used by the cleaner definitions -------------------------

def children(directory: str) -> list[str]:
    """Everything inside `directory` (the directory itself is kept)."""
    try:
        with os.scandir(directory) as it:
            return [e.path for e in it]
    except OSError:
        return []


def glob_in(directory: str, *patterns: str) -> list[str]:
    if not os.path.isdir(directory):
        return []
    base = glob.escape(directory)
    return [p for pat in patterns for p in glob.glob(os.path.join(base, pat), include_hidden=True)]


def existing(base: str, names) -> list[str]:
    paths = (os.path.join(base, n) for n in names)
    return [p for p in paths if os.path.lexists(p)]
