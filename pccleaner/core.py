"""Cleaning targets: a checkbox in the UI made of one or more parts.

A part is anything with `scan() -> Stats` and `clean() -> Stats`:
  Files    - files/folders on disk
  RegKeys  - registry MRU keys under HKCU
  Custom   - any pair of functions (SQLite history, ini files, Win32 calls...)
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

from . import fsutil, regutil
from .stats import Stats


class Files:
    """`paths` is evaluated on every scan/clean, so newly created files are picked up."""

    def __init__(self, paths: Callable[[], Iterable[str]], min_age_hours: float = 0):
        self.paths = paths
        self.min_age_hours = min_age_hours

    def _cutoff(self) -> float | None:
        return time.time() - self.min_age_hours * 3600 if self.min_age_hours else None

    def scan(self) -> Stats:
        stats, cutoff = Stats(), self._cutoff()
        for path in self.paths():
            fsutil.measure(path, stats, cutoff)
        return stats

    def clean(self) -> Stats:
        stats, cutoff = Stats(), self._cutoff()
        for path in self.paths():
            fsutil.remove(path, stats, cutoff)
        return stats


class RegKeys:
    def __init__(self, *keys: str, count: str = "max"):
        self.keys = keys
        self.count = count

    def scan(self) -> Stats:
        return Stats(entries=sum(regutil.count_entries(k, self.count) for k in self.keys))

    def clean(self) -> Stats:
        stats = self.scan()
        for key in self.keys:
            regutil.clear(key)
        return stats


class Custom:
    def __init__(self, scan: Callable[[], Stats], clean: Callable[[], Stats]):
        self.scan = scan
        self.clean = clean


@dataclass
class Target:
    id: str
    group: str
    name: str
    description: str
    parts: list = field(default_factory=list)
    default: bool = True          # ticked on first run
    admin: bool = False           # needs administrator rights
    processes: tuple[str, ...] = ()  # programs that must be closed to clean
    warning: str = ""             # shown when the user ticks the item

    def __post_init__(self):
        self.processes = tuple(p.lower() for p in self.processes)

    def scan(self) -> Stats:
        return self._run("scan")

    def clean(self) -> Stats:
        return self._run("clean")

    def _run(self, action: str) -> Stats:
        total = Stats()
        for part in self.parts:
            try:
                total.merge(getattr(part, action)())
            except Exception as exc:  # one failing part must not stop the others
                total.note(f"{type(exc).__name__}: {exc}")
        return total
