"""Command line: list / scan / clean without the GUI."""
from __future__ import annotations

import argparse
import os
import sys
import time

from . import platform as plat
from . import report
from .cleaners import build_catalog
from .stats import Stats, human_size


def _pick(targets, ids):
    if not ids:
        return [t for t in targets if t.default]
    by_id = {t.id: t for t in targets}
    unknown = [i for i in ids if i not in by_id]
    if unknown:
        raise SystemExit(f"Unknown id(s): {', '.join(unknown)}  (see: python -m pccleaner list)")
    return [by_id[i] for i in ids]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="pccleaner", description="Clean temp files, history and caches.")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("gui", help="open the window (default)")
    sub.add_parser("list", help="list cleanable items and their ids")
    scan = sub.add_parser("scan", help="show what would be deleted (deletes nothing)")
    scan.add_argument("ids", nargs="*", help="item ids (default: the default selection)")
    clean = sub.add_parser("clean", help="delete")
    clean.add_argument("ids", nargs="*", help="item ids (default: the default selection)")
    clean.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    sub.add_parser("winapp2-update", help="download the latest winapp2.ini community rules")
    large = sub.add_parser("large", help="list the largest files in a folder (deletes nothing)")
    large.add_argument("folder", nargs="?", default=os.environ.get("USERPROFILE", "."))
    large.add_argument("--min-mb", type=float, default=100, help="minimum size in MB (default 100)")
    large.add_argument("--days", type=float, default=0, help="only files not changed for this many days")
    large.add_argument("--top", type=int, default=30, help="how many to show (default 30)")
    shortcut = sub.add_parser(
        "install-shortcut",
        help="add a Start menu shortcut that runs PC Cleaner with Python "
             "(for PCs where Smart App Control blocks the exe)")
    shortcut.add_argument("--desktop", action="store_true", help="also add a desktop shortcut")
    shortcut.add_argument("--remove", action="store_true", help="remove the shortcuts instead")
    sub.add_parser("schedule", help="show the cleaning schedule")
    sub.add_parser("scheduled-run", help="clean the scheduled items without prompts (used by the task)")
    args = parser.parse_args(argv)

    if args.cmd in (None, "gui"):
        from .gui import run
        run()
        return 0

    if args.cmd == "install-shortcut":
        return plat.install_shortcuts(desktop=args.desktop, remove=args.remove)

    if args.cmd == "large":
        from . import largefiles
        found = largefiles.find(args.folder, int(args.min_mb * 1024 * 1024), args.days)
        for f in found[:args.top]:
            print(f"{human_size(f.size):>10}  {time.strftime('%Y-%m-%d', time.localtime(f.modified))}  {f.path}")
        print(f"\n{len(found):,} files of {args.min_mb:g} MB or more, {human_size(sum(f.size for f in found))} in total")
        return 0

    if args.cmd == "scheduled-run":
        from . import scheduler
        path = scheduler.run_scheduled()
        if sys.stdout:
            print(f"Report: {path}")
        return 0

    if args.cmd == "schedule":
        from . import scheduler, settings
        schedule = settings.load().get("schedule")
        nxt = scheduler.next_run()
        if not schedule or nxt is None:
            print("No cleaning schedule. Set one up from the window (Schedule...).")
        else:
            print(f"{scheduler.describe(schedule)}; next run: {nxt}")
            print("Items: " + ", ".join(schedule.get("items", [])))
        return 0

    if args.cmd == "winapp2-update":
        from .cleaners import winapp2
        print(f"Downloading {winapp2.WINAPP2_URL} ...")
        count = winapp2.download()
        detected = sum(1 for _ in winapp2.targets())
        print(f"Saved {count:,} rules to {winapp2.WINAPP2_FILE}; {detected:,} match programs on this PC.")
        return 0

    targets = build_catalog()
    if args.cmd == "list":
        for t in targets:
            flags = ("default" if t.default else "") + (" admin" if t.admin else "")
            print(f"{t.id:28} {t.group} / {t.name}  [{flags.strip()}]")
        return 0

    selected = _pick(targets, args.ids)
    admin = plat.is_admin()
    skipped_admin = [t for t in selected if t.admin and not admin]
    selected = [t for t in selected if t not in skipped_admin]
    for t in skipped_admin:
        print(f"skip  {t.id}: needs administrator rights")

    if args.cmd == "clean" and not args.yes:
        print("Will permanently clean:\n  " + "\n  ".join(f"{t.group} / {t.name}" for t in selected))
        if input("Continue? [y/N] ").strip().lower() != "y":
            return 1

    running = plat.running_processes()
    total = Stats()
    rows = []
    for t in selected:
        open_apps = [p for p in t.processes if p in running]
        if args.cmd == "clean" and open_apps:
            print(f"skip  {t.id}: close {', '.join(open_apps)} first")
            continue
        stats = t.scan() if args.cmd == "scan" else t.clean()
        rows.append((f"{t.group} / {t.name}", stats))
        total.merge(stats)
        extra = "; ".join(stats.messages + ([f"{stats.skipped} skipped"] if stats.skipped else []))
        print(f"{t.id:28} {stats.files:>7,} files {stats.entries:>6,} entries {human_size(stats.bytes):>10}"
              + (f"   {extra}" if extra else ""))
    verb = "can be freed" if args.cmd == "scan" else "freed"
    print(f"\nTotal: {human_size(total.bytes)} {verb}, {total.files:,} files, {total.entries:,} entries")
    if args.cmd == "clean":
        print(f"Report: {report.write('Cleaning (command line)', rows)}")
    return 0
