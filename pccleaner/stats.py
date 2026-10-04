"""Result counters shared by every cleaner."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Stats:
    files: int = 0      # files (or file-like items) found / deleted
    bytes: int = 0      # disk space they use
    entries: int = 0    # non-file items: registry MRU entries, history rows, ...
    skipped: int = 0    # items that could not be deleted (in use, access denied)
    messages: list[str] = field(default_factory=list)

    def merge(self, other: "Stats") -> "Stats":
        self.files += other.files
        self.bytes += other.bytes
        self.entries += other.entries
        self.skipped += other.skipped
        for msg in other.messages:
            self.note(msg)
        return self

    def note(self, msg: str) -> None:
        if msg not in self.messages:
            self.messages.append(msg)

    @property
    def found(self) -> int:
        return self.files + self.entries


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"
