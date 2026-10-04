"""Tkinter user interface: pick items on the left, Analyze, then Clean."""
from __future__ import annotations

import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from . import largefiles, report, scheduler, settings, winutil
from .cleaners import build_catalog, winapp2
from .core import Target
from .stats import Stats, human_size

ON, OFF, PARTIAL = "☑", "☐", "▣"


def _icon_path() -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, "assets", "pccleaner.ico")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PC Cleaner")
        self.geometry("1060x680")
        self.minsize(880, 540)
        try:
            self.iconbitmap(default=_icon_path())  # default= also applies to dialogs
        except tk.TclError:
            pass

        self.is_admin = winutil.is_admin()
        self.targets = build_catalog()
        self.by_id = {t.id: t for t in self.targets}
        self.checked = self._load_selection()
        self.events: queue.Queue = queue.Queue()
        self.busy = False
        self.mode = "scan"

        self._build_ui()
        self._refresh_tree()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._drain_events)

    # ---- layout --------------------------------------------------------------

    def _build_ui(self):
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Treeview", rowheight=24)

        root = ttk.Frame(self, padding=10)
        root.pack(fill="both", expand=True)

        header = ttk.Frame(root)
        header.pack(fill="x")
        ttk.Label(header, text="PC Cleaner", font=("Segoe UI", 16, "bold")).pack(side="left")
        self.winapp2_btn = ttk.Button(header, text="Update winapp2.ini", command=self._update_winapp2)
        self.winapp2_btn.pack(side="left", padx=(16, 0))
        ttk.Button(header, text="Schedule...", command=lambda: ScheduleDialog(self)).pack(side="left", padx=6)
        ttk.Button(header, text="Reports", command=self._open_reports).pack(side="left")
        ttk.Button(header, text="Large files...", command=lambda: LargeFilesWindow(self)).pack(side="left", padx=6)
        if self.is_admin:
            ttk.Label(header, text="Running as administrator", foreground="#2e7d32").pack(side="right")
        else:
            ttk.Button(header, text="Restart as administrator", command=self._elevate).pack(side="right")
            ttk.Label(header, text="Some Windows items need administrator rights.",
                      foreground="#a15c00").pack(side="right", padx=8)

        panes = ttk.PanedWindow(root, orient="horizontal")
        panes.pack(fill="both", expand=True, pady=8)

        # left: checklist
        left = ttk.Frame(panes)
        panes.add(left, weight=1)
        picks = ttk.Frame(left)
        picks.pack(side="bottom", fill="x", pady=(6, 0))
        ttk.Button(picks, text="Defaults", command=lambda: self._select("default")).pack(side="left")
        ttk.Button(picks, text="All", command=lambda: self._select("all")).pack(side="left", padx=4)
        ttk.Button(picks, text="None", command=lambda: self._select("none")).pack(side="left")
        self.desc = ttk.Label(left, text="Click an item to tick or untick it.", wraplength=320,
                              justify="left", foreground="#555")
        self.desc.pack(side="bottom", fill="x", pady=(6, 0))
        self.tree = ttk.Treeview(left, show="tree", selectmode="browse")
        sb = ttk.Scrollbar(left, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.tag_configure("disabled", foreground="#999")
        self.tree.tag_configure("group", font=("Segoe UI", 9, "bold"))
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<space>", lambda e: self._toggle(self.tree.focus()))
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self._populate_tree()

        # right: results + log
        right = ttk.Frame(panes)
        panes.add(right, weight=3)
        self.summary = ttk.Label(right, text="Press Analyze to see what can be cleaned.",
                                 font=("Segoe UI", 11, "bold"))
        self.summary.pack(fill="x", pady=(0, 6))
        cols = ("item", "found", "size", "status")
        res_frame = ttk.Frame(right)
        res_frame.pack(fill="both", expand=True)
        self.results = ttk.Treeview(res_frame, columns=cols, show="headings")
        for col, title, width, anchor in (("item", "Item", 260, "w"), ("found", "Found", 150, "e"),
                                          ("size", "Size", 90, "e"), ("status", "Status", 280, "w")):
            self.results.heading(col, text=title, anchor=anchor)
            self.results.column(col, width=width, anchor=anchor, stretch=(col in ("item", "status")))
        rsb = ttk.Scrollbar(res_frame, command=self.results.yview)
        self.results.configure(yscrollcommand=rsb.set)
        rsb.pack(side="right", fill="y")
        self.results.pack(side="left", fill="both", expand=True)

        self.log = ScrolledText(right, height=7, state="disabled", font=("Consolas", 9))
        self.log.pack(fill="x", pady=(6, 0))

        # bottom: progress + actions
        bottom = ttk.Frame(root)
        bottom.pack(fill="x")
        self.clean_btn = ttk.Button(bottom, text="Run Cleaner", command=lambda: self._start("clean"))
        self.clean_btn.pack(side="right")
        self.scan_btn = ttk.Button(bottom, text="Analyze", command=lambda: self._start("scan"))
        self.scan_btn.pack(side="right", padx=6)
        self.status = ttk.Label(bottom, text="Ready", width=40)
        self.status.pack(side="left")
        self.progress = ttk.Progressbar(bottom, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=8)

    # ---- checklist -------------------------------------------------------------

    def _usable(self, t: Target) -> bool:
        return self.is_admin or not t.admin

    def _populate_tree(self):
        self.tree.delete(*self.tree.get_children())
        groups: dict[str, str] = {}
        for t in self.targets:
            if t.group not in groups:
                community = t.group.startswith("winapp2")
                groups[t.group] = self.tree.insert("", "end", iid="g:" + t.group, open=not community,
                                                   tags=("group",))
            self.tree.insert(groups[t.group], "end", iid=t.id)
        self._refresh_tree()

    def _confirm_warnings(self, targets: list[Target]) -> bool:
        """Show winapp2 warnings for items about to be ticked. False = don't tick."""
        warned = [t for t in targets if t.warning and not self.checked.get(t.id)]
        if not warned:
            return True
        text = "\n\n".join(f"{t.name}: {t.warning}" for t in warned[:8])
        if len(warned) > 8:
            text += f"\n\n...and {len(warned) - 8} more."
        return messagebox.askokcancel("Warning", text, icon="warning", parent=self)

    def _refresh_tree(self):
        for group in self.tree.get_children():
            kids = [self.by_id[k] for k in self.tree.get_children(group)]
            usable = [t for t in kids if self._usable(t)]
            ticked = sum(self.checked.get(t.id, False) for t in usable)
            box = ON if usable and ticked == len(usable) else PARTIAL if ticked else OFF
            self.tree.item(group, text=f"{box}  {group[2:]}")
            for t in kids:
                if self._usable(t):
                    box = ON if self.checked.get(t.id) else OFF
                    self.tree.item(t.id, text=f"{box}  {t.name}", tags=())
                else:
                    self.tree.item(t.id, text=f"{OFF}  {t.name}  (admin)", tags=("disabled",))

    def _toggle(self, iid: str):
        if self.busy or not iid:
            return
        if iid.startswith("g:"):
            kids = [self.by_id[k] for k in self.tree.get_children(iid) if self._usable(self.by_id[k])]
            new = not all(self.checked.get(t.id) for t in kids)
            if new and not self._confirm_warnings(kids):
                return
            for t in kids:
                self.checked[t.id] = new
        elif self._usable(self.by_id[iid]):
            t = self.by_id[iid]
            if not self.checked.get(iid) and not self._confirm_warnings([t]):
                return
            self.checked[iid] = not self.checked.get(iid)
        self._refresh_tree()
        self._save_selection()

    def _on_tree_click(self, event):
        iid = self.tree.identify_row(event.y)
        if iid and "indicator" not in self.tree.identify_element(event.x, event.y):
            self._toggle(iid)

    def _on_tree_select(self, _event):
        iid = self.tree.focus()
        if iid in self.by_id:
            t = self.by_id[iid]
            note = "\nRequires administrator rights." if t.admin else ""
            self.desc.configure(text=t.description + note)

    def _select(self, which: str):
        if self.busy:
            return
        for t in self.targets:
            # "All" covers the built-in items; community winapp2 rules are ticked deliberately
            everything = not t.id.startswith("w2.") or self.checked.get(t.id, False)
            self.checked[t.id] = {"all": everything, "none": False, "default": t.default}[which]
        self._refresh_tree()
        self._save_selection()

    def _update_winapp2(self):
        if self.busy:
            return
        if not messagebox.askyesno(
                "winapp2.ini",
                "Download the latest winapp2.ini - community cleaning rules for thousands of programs - "
                f"from github.com/MoscaDotTo/Winapp2?\n\nIt is saved to {winapp2.WINAPP2_FILE}. "
                "Rules for programs on this PC appear unticked in 'winapp2' groups.", parent=self):
            return
        self.busy = True
        self.winapp2_btn.state(["disabled"])
        self.status.configure(text="Downloading winapp2.ini...")

        def work():
            try:
                count = winapp2.download()
                self.events.put(("winapp2", count, build_catalog()))
            except Exception as exc:
                self.events.put(("winapp2", exc, None))

        threading.Thread(target=work, daemon=True).start()

    def _winapp2_done(self, result, catalog):
        self.busy = False
        self.winapp2_btn.state(["!disabled"])
        self.status.configure(text="Ready")
        if isinstance(result, Exception):
            messagebox.showerror("winapp2.ini", f"Download failed:\n{result}", parent=self)
            return
        self.targets = catalog
        self.by_id = {t.id: t for t in catalog}
        self.checked = self._load_selection() | {k: v for k, v in self.checked.items() if k in self.by_id}
        self._populate_tree()
        found = sum(1 for t in catalog if t.id.startswith("w2."))
        self._log(f"winapp2.ini updated: {result:,} rules, {found:,} match programs on this PC.")
        messagebox.showinfo("winapp2.ini", f"Downloaded {result:,} rules.\n{found:,} match programs on this PC "
                            "and are listed (unticked) under the 'winapp2' groups.", parent=self)

    def _load_selection(self) -> dict[str, bool]:
        selection = {t.id: t.default for t in self.targets}
        saved = settings.load().get("checked")
        if isinstance(saved, dict):
            selection.update({k: bool(v) for k, v in saved.items() if k in selection})
        return selection

    def _save_selection(self):
        try:
            settings.update(checked=self.checked)
        except OSError:
            pass

    # ---- running a scan / clean ------------------------------------------------------

    def _start(self, mode: str):
        if self.busy:
            return
        selected = [t for t in self.targets if self.checked.get(t.id) and self._usable(t)]
        if not selected:
            messagebox.showinfo("PC Cleaner", "Tick at least one item first.", parent=self)
            return

        if mode == "clean":
            running = winutil.running_processes()
            blocked = [t for t in selected if any(p in running for p in t.processes)]
            if blocked:
                apps = sorted({p for t in blocked for p in t.processes if p in running})
                names = "\n".join(f"  • {t.group}: {t.name}" for t in blocked)
                if not messagebox.askyesno(
                        "Programs are running",
                        f"These programs are open: {', '.join(apps)}\n\n"
                        f"Close them to clean their data. These items will be skipped:\n{names}\n\n"
                        "Continue with the remaining items?", parent=self):
                    return
                selected = [t for t in selected if t not in blocked]
                if not selected:
                    return
            if not messagebox.askyesno(
                    "Confirm cleaning",
                    f"Permanently delete the data for {len(selected)} selected item(s)?\n\n"
                    "This cannot be undone.", icon="warning", parent=self):
                return

        self.busy = True
        self.mode = mode
        self.scan_btn.state(["disabled"])
        self.clean_btn.state(["disabled"])
        self.results.delete(*self.results.get_children())
        self.run_rows: list[tuple[str, Stats]] = []
        self.progress.configure(maximum=len(selected), value=0)
        self.summary.configure(text="Analyzing..." if mode == "scan" else "Cleaning...")
        self._log(f"--- {'Analysis' if mode == 'scan' else 'Cleaning'} started ---")
        threading.Thread(target=self._work, args=(mode, selected), daemon=True).start()

    def _work(self, mode: str, targets: list[Target]):
        running = winutil.running_processes() if mode == "scan" else set()
        total = Stats()
        for i, t in enumerate(targets):
            self.events.put(("progress", i, t))
            try:
                stats = t.scan() if mode == "scan" else t.clean()
            except Exception as exc:
                stats = Stats()
                stats.note(f"Failed: {exc}")
            open_apps = [p for p in t.processes if p in running]
            if open_apps:
                stats.note(f"Close {', '.join(open_apps)} before cleaning")
            total.merge(stats)
            self.events.put(("result", t, stats))
        self.events.put(("done", mode, total))

    def _drain_events(self):
        try:
            while True:
                self._handle(*self.events.get_nowait())
        except queue.Empty:
            pass
        self.after(80, self._drain_events)

    def _handle(self, kind, a, b):
        if kind == "winapp2":
            self._winapp2_done(a, b)
        elif kind == "progress":
            self.progress.configure(value=a)
            self.status.configure(text=f"{b.group}: {b.name}")
        elif kind == "result":
            self.progress.step(1)
            self.run_rows.append((f"{a.group} / {a.name}", b))
            found = []
            if b.files:
                found.append(f"{b.files:,} files")
            if b.entries:
                found.append(f"{b.entries:,} entries")
            status = "; ".join(b.messages)
            if b.skipped:
                status = (f"{b.skipped:,} in use / denied - skipped" + ("; " + status if status else ""))
            if not status:
                status = "Nothing found" if not b.found else ("OK" if self.mode == "scan" else "Cleaned")
            self.results.insert("", "end", values=(f"{a.group}: {a.name}", ", ".join(found) or "-",
                                                   human_size(b.bytes) if b.bytes else "-", status))
            for msg in b.messages:
                self._log(f"{a.group} / {a.name}: {msg}")
        elif kind == "done":
            mode, total = a, b
            self.busy = False
            self.scan_btn.state(["!disabled"])
            self.clean_btn.state(["!disabled"])
            self.status.configure(text="Ready")
            verb = "can be freed" if mode == "scan" else "freed"
            self.summary.configure(
                text=f"{'Analysis' if mode == 'scan' else 'Cleaning'} complete — {human_size(total.bytes)} {verb}"
                     f"  ({total.files:,} files, {total.entries:,} history entries)")
            if total.skipped:
                self._log(f"{total.skipped:,} items were in use or access was denied and were skipped.")
            if mode == "clean":
                try:
                    self._log(f"Report saved: {report.write('Cleaning', self.run_rows)}")
                except OSError as exc:
                    self._log(f"Could not save report: {exc}")
            self._log(f"--- {'Analysis' if mode == 'scan' else 'Cleaning'} finished ---")

    def _log(self, line: str):
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    # ---- misc -----------------------------------------------------------------

    def _open_reports(self):
        os.makedirs(settings.LOG_DIR, exist_ok=True)
        os.startfile(settings.LOG_DIR)

    def ticked_targets(self) -> list[Target]:
        return [t for t in self.targets if self.checked.get(t.id) and self._usable(t)]

    def _elevate(self):
        if winutil.relaunch_as_admin():
            self._on_close()
        else:
            messagebox.showwarning("PC Cleaner", "Could not restart as administrator.", parent=self)

    def _on_close(self):
        self._save_selection()
        self.destroy()


class ScheduleDialog(tk.Toplevel):
    """Create, change or remove the scheduled cleaning task."""

    def __init__(self, app: App):
        super().__init__(app)
        self.app = app
        self.title("Scheduled cleaning")
        self.resizable(False, False)
        self.transient(app)
        self.grab_set()

        saved = settings.load().get("schedule") or {}
        self.items = app.ticked_targets()

        frame = ttk.Frame(self, padding=14)
        frame.pack(fill="both", expand=True)
        self.status = ttk.Label(frame, text="Checking current schedule...", font=("Segoe UI", 10, "bold"))
        self.status.grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 10))

        ttk.Label(frame, text="Run:").grid(row=1, column=0, sticky="w")
        self.frequency = tk.StringVar(value=saved.get("frequency", "weekly"))
        ttk.Radiobutton(frame, text="Every day", value="daily", variable=self.frequency,
                        command=self._sync).grid(row=1, column=1, sticky="w")
        ttk.Radiobutton(frame, text="Every week on", value="weekly", variable=self.frequency,
                        command=self._sync).grid(row=2, column=1, sticky="w")
        self.day = tk.StringVar(value=saved.get("day", "Sunday"))
        self.day_box = ttk.Combobox(frame, textvariable=self.day, values=scheduler.DAYS, state="readonly", width=12)
        self.day_box.grid(row=2, column=2, sticky="w", padx=4)

        hour, minute = (saved.get("time") or "20:00").split(":")
        ttk.Label(frame, text="At:").grid(row=3, column=0, sticky="w", pady=(8, 0))
        at = ttk.Frame(frame)
        at.grid(row=3, column=1, columnspan=2, sticky="w", pady=(8, 0))
        self.hour = ttk.Spinbox(at, from_=0, to=23, width=4, format="%02.0f", wrap=True)
        self.hour.set(hour)
        self.hour.pack(side="left")
        ttk.Label(at, text=":").pack(side="left")
        self.minute = ttk.Spinbox(at, from_=0, to=59, width=4, format="%02.0f", wrap=True, increment=5)
        self.minute.set(minute)
        self.minute.pack(side="left")
        ttk.Label(at, text="(if the PC is off, it runs at next start-up)", foreground="#555").pack(side="left", padx=8)

        ttk.Label(frame, text=f"It will clean the {len(self.items)} item(s) ticked now:").grid(
            row=4, column=0, columnspan=4, sticky="w", pady=(12, 4))
        box = tk.Listbox(frame, height=8, width=62, activestyle="none")
        for t in self.items:
            box.insert("end", f"{t.group}: {t.name}")
        box.grid(row=5, column=0, columnspan=4, sticky="we")

        notes = ["Programs that are open at that time are skipped. Each run saves a report (Reports button)."]
        if not app.is_admin:
            notes.append("Items marked (admin) are not included - open PC Cleaner as administrator "
                         "before saving the schedule to include them.")
        ttk.Label(frame, text="\n".join(notes), wraplength=440, justify="left", foreground="#555").grid(
            row=6, column=0, columnspan=4, sticky="w", pady=(8, 0))

        buttons = ttk.Frame(frame)
        buttons.grid(row=7, column=0, columnspan=4, sticky="e", pady=(14, 0))
        ttk.Button(buttons, text="Close", command=self.destroy).pack(side="right")
        self.remove_btn = ttk.Button(buttons, text="Turn off", command=self._remove)
        self.remove_btn.pack(side="right", padx=6)
        ttk.Button(buttons, text="Save schedule", command=self._save).pack(side="right")

        self._sync()
        threading.Thread(target=self._load_status, daemon=True).start()

    def _sync(self):
        self.day_box.configure(state="readonly" if self.frequency.get() == "weekly" else "disabled")

    def _load_status(self):
        saved = settings.load().get("schedule")
        nxt = scheduler.next_run()
        text = (f"Current: {scheduler.describe(saved)} - next run {nxt}" if saved and nxt
                else "No schedule is set.")
        self.after(0, lambda: self.winfo_exists() and self.status.configure(text=text))

    def _time(self) -> str | None:
        try:
            hour, minute = int(self.hour.get()), int(self.minute.get())
        except ValueError:
            return None
        return f"{hour:02d}:{minute:02d}" if 0 <= hour <= 23 and 0 <= minute <= 59 else None

    def _save(self):
        time_of_day = self._time()
        if time_of_day is None:
            messagebox.showerror("Scheduled cleaning", "Enter a valid time (hour 0-23, minute 0-59).", parent=self)
            return
        if not self.items:
            messagebox.showinfo("Scheduled cleaning", "Tick the items to clean in the main window first.", parent=self)
            return
        try:
            scheduler.install(self.frequency.get(), time_of_day, self.day.get(), [t.id for t in self.items])
        except Exception as exc:
            messagebox.showerror("Scheduled cleaning", f"Could not create the scheduled task:\n{exc}", parent=self)
            return
        self.app._log(f"Schedule saved: {scheduler.describe(settings.load()['schedule'])}, "
                      f"{len(self.items)} item(s).")
        self.status.configure(text="Saved. Checking next run time...")
        threading.Thread(target=self._load_status, daemon=True).start()

    def _remove(self):
        try:
            scheduler.remove()
        except Exception as exc:
            messagebox.showerror("Scheduled cleaning", f"Could not remove the task:\n{exc}", parent=self)
            return
        self.app._log("Schedule turned off.")
        self.status.configure(text="No schedule is set.")


class LargeFilesWindow(tk.Toplevel):
    """Find the biggest files in a folder and send chosen ones to the Recycle Bin."""

    COLUMNS = (("name", "Name", 260, "w"), ("size", "Size", 90, "e"),
               ("modified", "Modified", 130, "w"), ("folder", "Folder", 380, "w"))

    def __init__(self, app: App):
        super().__init__(app)
        self.app = app
        self.title("Large File Finder")
        self.geometry("960x600")
        self.minsize(720, 420)
        self.files: dict[str, largefiles.FileInfo] = {}
        self.cancel = threading.Event()
        self.events: queue.Queue = queue.Queue()
        self.sort_key, self.sort_desc = "size", True

        frame = ttk.Frame(self, padding=10)
        frame.pack(fill="both", expand=True)

        opts = ttk.Frame(frame)
        opts.pack(fill="x")
        ttk.Label(opts, text="Folder:").grid(row=0, column=0, sticky="w")
        self.folder = tk.StringVar(value=os.environ.get("USERPROFILE", "C:\\"))
        ttk.Entry(opts, textvariable=self.folder, width=60).grid(row=0, column=1, sticky="we", padx=4)
        ttk.Button(opts, text="Browse...", command=self._browse).grid(row=0, column=2)
        opts.columnconfigure(1, weight=1)

        filters = ttk.Frame(frame)
        filters.pack(fill="x", pady=(8, 0))
        ttk.Label(filters, text="At least").pack(side="left")
        self.min_mb = ttk.Spinbox(filters, values=(10, 50, 100, 250, 500, 1000, 2000, 5000), width=6)
        self.min_mb.set(100)
        self.min_mb.pack(side="left", padx=4)
        ttk.Label(filters, text="MB,  not changed for").pack(side="left")
        self.min_days = ttk.Spinbox(filters, values=(0, 30, 90, 180, 365, 730), width=5)
        self.min_days.set(0)
        self.min_days.pack(side="left", padx=4)
        ttk.Label(filters, text="days (0 = any)").pack(side="left")
        self.skip_system = tk.BooleanVar(value=True)
        ttk.Checkbutton(filters, text="Skip Windows and program folders",
                        variable=self.skip_system).pack(side="left", padx=16)
        self.find_btn = ttk.Button(filters, text="Find", command=self._find)
        self.find_btn.pack(side="right")
        self.stop_btn = ttk.Button(filters, text="Stop", command=self.cancel.set, state="disabled")
        self.stop_btn.pack(side="right", padx=6)

        table = ttk.Frame(frame)
        table.pack(fill="both", expand=True, pady=8)
        self.tree = ttk.Treeview(table, columns=[c[0] for c in self.COLUMNS], show="headings", selectmode="extended")
        for col, title, width, anchor in self.COLUMNS:
            self.tree.heading(col, text=title, anchor=anchor, command=lambda c=col: self._sort(c))
            self.tree.column(col, width=width, anchor=anchor, stretch=col in ("name", "folder"))
        sb = ttk.Scrollbar(table, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.tag_configure("app", foreground="#8a6d00")
        self.tree.bind("<Double-1>", lambda e: self._show())
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._update_selection())

        bottom = ttk.Frame(frame)
        bottom.pack(fill="x")
        self.status = ttk.Label(bottom, text="Choose a folder and press Find. Nothing is deleted without asking.")
        self.status.pack(side="left")
        self.recycle_btn = ttk.Button(bottom, text="Send to Recycle Bin", command=self._recycle, state="disabled")
        self.recycle_btn.pack(side="right")
        ttk.Button(bottom, text="Show in Explorer", command=self._show).pack(side="right", padx=6)

        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(100, self._drain)

    def _browse(self):
        from tkinter import filedialog
        chosen = filedialog.askdirectory(parent=self, initialdir=self.folder.get(), mustexist=True)
        if chosen:
            self.folder.set(os.path.normpath(chosen))

    def _find(self):
        root = self.folder.get().strip()
        if not os.path.isdir(root):
            messagebox.showerror("Large File Finder", "That folder does not exist.", parent=self)
            return
        try:
            min_bytes = int(float(self.min_mb.get()) * 1024 * 1024)
            min_days = float(self.min_days.get() or 0)
        except ValueError:
            messagebox.showerror("Large File Finder", "Enter numbers for size and days.", parent=self)
            return
        self.cancel.clear()
        self.find_btn.state(["disabled"])
        self.stop_btn.state(["!disabled"])
        self.tree.delete(*self.tree.get_children())
        self.files.clear()

        def work():
            found = largefiles.find(root, min_bytes, min_days, self.skip_system.get(), self.cancel,
                                    lambda folders, matched: self.events.put(("progress", folders, matched)))
            self.events.put(("done", found, self.cancel.is_set()))

        threading.Thread(target=work, daemon=True).start()

    def _drain(self):
        try:
            while True:
                kind, a, b = self.events.get_nowait()
                if kind == "progress":
                    self.status.configure(text=f"Searching... {a:,} folders, {b:,} large files")
                else:
                    self._show_results(a, stopped=b)
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(100, self._drain)

    def _show_results(self, found, stopped: bool):
        self.find_btn.state(["!disabled"])
        self.stop_btn.state(["disabled"])
        self.files = {f.path: f for f in found}
        self._fill()
        total = sum(f.size for f in found)
        capped = f" (largest {largefiles.MAX_RESULTS:,} shown)" if len(found) >= largefiles.MAX_RESULTS else ""
        self.status.configure(text=f"{'Stopped - ' if stopped else ''}{len(found):,} files, "
                                   f"{human_size(total)} in total{capped}.")

    def _fill(self):
        self.tree.delete(*self.tree.get_children())
        key = {"name": lambda f: os.path.basename(f.path).lower(), "size": lambda f: f.size,
               "modified": lambda f: f.modified, "folder": lambda f: os.path.dirname(f.path).lower()}[self.sort_key]
        for f in sorted(self.files.values(), key=key, reverse=self.sort_desc):
            modified = time.strftime("%Y-%m-%d %H:%M", time.localtime(f.modified))
            app_file = largefiles.is_app_file(f.path)
            name = os.path.basename(f.path) + ("   (app file)" if app_file else "")
            self.tree.insert("", "end", iid=f.path, tags=("app",) if app_file else (),
                             values=(name, human_size(f.size), modified, os.path.dirname(f.path)))

    def _sort(self, column: str):
        self.sort_desc = not self.sort_desc if self.sort_key == column else column in ("size", "modified")
        self.sort_key = column
        self._fill()

    def _update_selection(self):
        selected = self.tree.selection()
        self.recycle_btn.state(["!disabled"] if selected else ["disabled"])
        if selected:
            size = sum(self.files[p].size for p in selected)
            self.status.configure(text=f"{len(selected):,} selected, {human_size(size)}")

    def _show(self):
        selected = self.tree.selection()
        if selected:
            winutil.show_in_explorer(selected[0])

    def _recycle(self):
        paths = list(self.tree.selection())
        if not paths:
            return
        size = sum(self.files[p].size for p in paths)
        names = "\n".join(f"  \u2022 {os.path.basename(p)}" for p in paths[:10])
        more = f"\n  ...and {len(paths) - 10} more" if len(paths) > 10 else ""
        app_files = sum(1 for p in paths if largefiles.is_app_file(p))
        caution = (f"\n\nCAUTION: {app_files} of these are app files (in AppData, program folders or hidden "
                   "folders). Removing them can break the program that uses them - uninstall the "
                   "program or use its own settings instead if unsure.") if app_files else ""
        if not messagebox.askyesno(
                "Send to Recycle Bin",
                f"Move {len(paths):,} file(s), {human_size(size)}, to the Recycle Bin?\n\n{names}{more}{caution}\n\n"
                "You can restore them from the Recycle Bin. Space is freed when the bin is emptied.",
                icon="warning", parent=self):
            return
        left = set(winutil.send_to_recycle_bin(paths, hwnd=self.winfo_id()))
        moved = [p for p in paths if p not in left]
        if moved:
            report.write("Large files sent to Recycle Bin",
                         [(p, Stats(files=1, bytes=self.files[p].size)) for p in moved])
            self.app._log(f"Sent {len(moved):,} large file(s) to the Recycle Bin.")
        for p in moved:
            self.tree.delete(p)
            del self.files[p]
        msg = f"Moved {len(moved):,} file(s) to the Recycle Bin."
        if left:
            msg += f"\n{len(left):,} file(s) were not moved (in use, cancelled or access denied)."
        self.status.configure(text=msg.replace("\n", " "))
        if left:
            messagebox.showwarning("Large File Finder", msg, parent=self)

    def _close(self):
        self.cancel.set()
        self.destroy()


def run():
    App().mainloop()
