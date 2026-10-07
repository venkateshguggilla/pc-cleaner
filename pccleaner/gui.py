"""User interface (tkinter + ttkbootstrap): pick items on the left, Analyze, then Clean."""
from __future__ import annotations

import ctypes
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox

import ttkbootstrap as tb

from . import largefiles, report, scheduler, settings, winutil
from .cleaners import build_catalog, winapp2
from .core import Target
from .stats import Stats, human_size

ON, OFF, PARTIAL = "\u2611", "\u2610", "\u25a3"
THEMES = {"light": "bootstrap-light", "dark": "bootstrap-dark"}
FONT = "Segoe UI"


def _asset(name: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, "assets", name)


_scale = 1.0  # display scaling (2.0 at 200%); set once the main window exists


def px(n: float) -> int:
    """Convert a layout size in 100%-scale pixels to real pixels for this display."""
    return int(round(n * _scale))


def _geometry(window, size, minsize):
    window.geometry(f"{px(size[0])}x{px(size[1])}")
    window.minsize(px(minsize[0]), px(minsize[1]))


def _colors():
    return tb.Style().colors


def _mix(color: str, other: str, amount: float) -> str:
    """Blend two #rrggbb colours: amount=0 gives `color`, 1 gives `other`."""
    a = [int(color[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(other[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * amount):02x}" for x, y in zip(a, b))


def _is_dark() -> bool:
    bg = _colors().bg
    return sum(int(bg[i:i + 2], 16) for i in (1, 3, 5)) < 384


def _match_title_bar(window) -> None:
    """Dark title bar in dark mode (the window frame isn't drawn by tkinter)."""
    dark = _is_dark()
    if getattr(window, "_dark_frame", False) == dark:
        return  # already matching (windows start with a light frame)
    try:
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id()) or window.winfo_id()
        winutil.set_dark_title_bar(hwnd, dark)
        window._dark_frame = dark
        if window.winfo_viewable():
            # Windows 10 only repaints the frame when the window is shown again
            window.withdraw()
            window.deiconify()
    except (OSError, tk.TclError, AttributeError):
        pass


APP_NAMES = {
    "chrome.exe": "Google Chrome", "msedge.exe": "Microsoft Edge", "brave.exe": "Brave", "vivaldi.exe": "Vivaldi",
    "opera.exe": "Opera", "ulaa.exe": "Zoho Ulaa", "firefox.exe": "Mozilla Firefox", "vlc.exe": "VLC",
    "wmplayer.exe": "Windows Media Player", "mpc-hc.exe": "MPC-HC", "mpc-hc64.exe": "MPC-HC",
    "mpc-be.exe": "MPC-BE", "mpc-be64.exe": "MPC-BE", "potplayermini.exe": "PotPlayer",
    "potplayermini64.exe": "PotPlayer", "winword.exe": "Microsoft Word", "excel.exe": "Microsoft Excel",
    "powerpnt.exe": "Microsoft PowerPoint", "msaccess.exe": "Microsoft Access", "mspub.exe": "Microsoft Publisher",
    "visio.exe": "Microsoft Visio", "onenote.exe": "Microsoft OneNote", "notepad++.exe": "Notepad++",
    "code.exe": "Visual Studio Code", "code - insiders.exe": "VS Code Insiders", "vscodium.exe": "VSCodium",
    "cursor.exe": "Cursor", "photos.exe": "Photos", "microsoft.photos.exe": "Photos",
    "mspaint.exe": "Paint", "wordpad.exe": "WordPad",
}


def app_name(exe: str) -> str:
    return APP_NAMES.get(exe.lower(), exe.rsplit(".", 1)[0])


def _app_list(exes) -> str:
    return "\n".join(f"  • {name}" for name in sorted({app_name(p) for p in exes}))


def _warning_text() -> str:
    """Bootstrap's warning yellow is unreadable as text on white; darken it in light mode."""
    c = _colors()
    return c.warning if _is_dark() else _mix(c.warning, "#000000", 0.45)


def _scrolled(parent, widget_factory):
    """Pack a widget with a vertical scrollbar into `parent`; returns the widget."""
    widget = widget_factory(parent)
    bar = tb.Scrollbar(parent, command=widget.yview, bootstyle="round")
    widget.configure(yscrollcommand=bar.set)
    bar.pack(side="right", fill="y")
    widget.pack(side="left", fill="both", expand=True)
    return widget


class App(tb.Window):
    def __init__(self):
        self.ui_theme = settings.load().get("theme", "light")
        super().__init__(title="PC Cleaner", themename=THEMES.get(self.ui_theme, THEMES["light"]), iconphoto=None)
        global _scale
        _scale = max(1.0, self.winfo_fpixels("1i") / 96)
        # fit the screen: 1140x730 at 100%, smaller if the (scaled) screen is smaller
        width = min(1140, int(self.winfo_screenwidth() / _scale * 0.92))
        height = min(730, int(self.winfo_screenheight() / _scale * 0.85))
        _geometry(self, (width, height), (min(940, width), min(580, height)))
        if hasattr(self, "place_window_center"):
            self.place_window_center()
        try:
            self.iconbitmap(default=_asset("pccleaner.ico"))  # default= also applies to dialogs
        except tk.TclError:
            pass

        self.is_admin = winutil.is_admin()
        self.targets = build_catalog()
        self.by_id = {t.id: t for t in self.targets}
        self.checked = self._load_selection()
        self.events: queue.Queue = queue.Queue()
        self.working = False
        self.mode = "scan"
        self.theme_listeners: list = []

        self._build_ui()
        self._apply_colors()
        self._refresh_tree()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._drain_job = self.after(100, self._drain_events)

    # ---- layout --------------------------------------------------------------

    def _build_ui(self):
        style = tb.Style()
        style.configure("Treeview", rowheight=px(26))
        style.configure("Treeview.Heading", font=(FONT, 9, "bold"))

        root = tb.Frame(self, padding=(px(18), px(14)))
        root.pack(fill="both", expand=True)

        # header: logo + title on the left, tools + admin + theme on the right
        header = tb.Frame(root)
        header.pack(fill="x")
        try:
            logo = tk.PhotoImage(file=_asset("pccleaner.png"))  # 256 px
            self.logo = logo.subsample(max(1, round(256 / px(40))))
            tb.Label(header, image=self.logo).pack(side="left", padx=(0, px(12)))
        except tk.TclError:
            pass
        titles = tb.Frame(header)
        titles.pack(side="left")
        tb.Label(titles, text="PC Cleaner", font=(FONT, 16, "bold")).pack(anchor="w")
        tb.Label(titles, text="Remove junk files and privacy traces", bootstyle="secondary",
                 font=(FONT, 9)).pack(anchor="w")

        self.theme_btn = tb.Button(header, bootstyle="secondary-ghost", icon_only=True,
                                   icon="sun" if self.ui_theme == "dark" else "moon-stars",
                                   command=self._toggle_theme)
        self.theme_btn.pack(side="right")
        tb.ToolTip(self.theme_btn, text="Switch light / dark theme")
        if self.is_admin:
            tb.Label(header, text="  Administrator  ", bootstyle="inverse-success",
                     font=(FONT, 9, "bold")).pack(side="right", padx=8)
        else:
            admin = tb.Button(header, text="Run as administrator", bootstyle="warning-outline",
                              icon="shield-lock", command=self._elevate)
            admin.pack(side="right", padx=8)
            tb.ToolTip(admin, text="Some Windows items (system temp, logs, update caches) need administrator rights.")
        tb.Separator(header, orient="vertical").pack(side="right", fill="y", padx=6, pady=6)
        tools = (("winapp2 rules", "cloud-download", self._update_winapp2,
                  "Download community cleaning rules for thousands of programs"),
                 ("Reports", "journal-text", self._open_reports, "Open the folder with cleaning reports"),
                 ("Schedule", "calendar-check", lambda: ScheduleDialog(self), "Clean automatically every day or week"),
                 ("Large files", "hdd", lambda: LargeFilesWindow(self), "Find the biggest files on your PC"))
        for text, icon, command, tip in tools:
            btn = tb.Button(header, text=text, icon=icon, bootstyle="secondary-ghost", command=command)
            btn.pack(side="right", padx=2)
            tb.ToolTip(btn, text=tip)
            if icon == "cloud-download":
                self.winapp2_btn = btn

        tb.Separator(root).pack(fill="x", pady=px(10))

        panes = tb.Panedwindow(root, orient="horizontal")
        panes.pack(fill="both", expand=True)

        # left: checklist card
        left = tb.Labelframe(panes, text=" What to clean ", padding=px(8), width=px(340))
        panes.add(left, weight=1)
        picks = tb.Frame(left)
        picks.pack(side="bottom", fill="x", pady=(px(8), 0))
        for text, which, icon in (("Recommended", "default", "check-all"), ("All", "all", "check2-square"),
                                  ("None", "none", "square")):
            tb.Button(picks, text=text, icon=icon, bootstyle="secondary-outline",
                      command=lambda w=which: self._select(w)).pack(side="left", padx=(0, px(6)))
        self.desc = tb.Label(left, text="Click an item to tick or untick it. Select it to see what it does.",
                             wraplength=px(320), justify="left", bootstyle="secondary", font=(FONT, 9))
        self.desc.pack(side="bottom", fill="x", pady=(px(8), 0))
        self.desc.bind("<Configure>", lambda e: self.desc.configure(wraplength=max(px(100), e.width - px(4))))
        tree_box = tb.Frame(left)
        tree_box.pack(fill="both", expand=True)
        self.tree = _scrolled(tree_box, lambda p: tb.Treeview(p, show="tree", selectmode="browse"))
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<space>", lambda e: self._toggle(self.tree.focus()))
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self._populate_tree()

        # right: summary + results + activity
        right = tb.Frame(panes, padding=(px(14), 0, 0, 0))
        panes.add(right, weight=3)
        hero = tb.Frame(right)
        hero.pack(fill="x", pady=(0, px(8)))
        self.hero_value = tb.Label(hero, text="Ready", font=(FONT, 20, "bold"), bootstyle="primary")
        self.hero_value.pack(side="left")
        self.hero_caption = tb.Label(hero, text="Press Analyze to see how much space can be freed.",
                                     bootstyle="secondary", font=(FONT, 10))
        self.hero_caption.pack(side="left", padx=px(12), pady=(px(8), 0))

        res_box = tb.Frame(right)
        res_box.pack(fill="both", expand=True)
        self.results = _scrolled(res_box, lambda p: tb.Treeview(p, columns=("item", "found", "size", "status"),
                                                                 show="headings"))
        for col, title, width, anchor in (("item", "Item", 290, "w"), ("found", "Found", 140, "e"),
                                          ("size", "Size", 90, "e"), ("status", "    Status", 220, "w")):
            self.results.heading(col, text=title, anchor=anchor)
            self.results.column(col, width=px(width), minwidth=px(60), anchor=anchor,
                                stretch=(col in ("item", "status")))

        activity = tb.Labelframe(right, text=" Activity ", padding=px(6))
        activity.pack(fill="x", pady=(px(10), 0))
        self.log = _scrolled(activity, lambda p: tb.Text(p, height=4, wrap="word", font=("Consolas", 9),
                                                         relief="flat", state="disabled"))

        # footer: status, progress, actions
        tb.Separator(root).pack(fill="x", pady=(px(12), px(10)))
        bottom = tb.Frame(root)
        bottom.pack(fill="x")
        self.clean_btn = tb.Button(bottom, text="Run Cleaner", icon="stars", bootstyle="primary",
                                   command=lambda: self._start("clean"))
        self.clean_btn.pack(side="right", ipadx=px(10))
        self.scan_btn = tb.Button(bottom, text="Analyze", icon="search", bootstyle="primary-outline",
                                  command=lambda: self._start("scan"))
        self.scan_btn.pack(side="right", padx=px(8), ipadx=px(10))
        self.status = tb.Label(bottom, text="Ready", bootstyle="secondary", width=36)
        self.status.pack(side="left")
        self.progress = tb.Progressbar(bottom, mode="determinate", bootstyle="success-striped")
        self.progress.pack(side="left", fill="x", expand=True, padx=px(12))

    def _apply_colors(self):
        """Colours that ttk tags can't take from the theme automatically."""
        c = _colors()
        # soft primary tint for selected rows instead of the heavy default
        tint = _mix(c.primary, c.bg, 0.65 if _is_dark() else 0.82)
        tb.Style().map("Treeview", background=[("selected", tint)], foreground=[("selected", c.fg)])
        self.tree.tag_configure("group", font=(FONT, 10, "bold"))
        self.tree.tag_configure("disabled", foreground=c.secondary)
        self.results.tag_configure("ok", foreground=c.fg)
        self.results.tag_configure("cleaned", foreground=c.success)
        self.results.tag_configure("note", foreground=_warning_text())
        self.results.tag_configure("empty", foreground=c.secondary)
        _match_title_bar(self)
        for listener in list(self.theme_listeners):
            listener()

    def _toggle_theme(self):
        self.ui_theme = "light" if self.ui_theme == "dark" else "dark"
        tb.Style().theme_use(THEMES[self.ui_theme])
        self.theme_btn.configure(icon="sun" if self.ui_theme == "dark" else "moon-stars")
        self._apply_colors()
        try:
            settings.update(theme=self.ui_theme)
        except OSError:
            pass

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
        if self.working or not iid:
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
        if self.working:
            return
        for t in self.targets:
            # "All" covers the built-in items; community winapp2 rules are ticked deliberately
            everything = not t.id.startswith("w2.") or self.checked.get(t.id, False)
            self.checked[t.id] = {"all": everything, "none": False, "default": t.default}[which]
        self._refresh_tree()
        self._save_selection()

    def _update_winapp2(self):
        if self.working:
            return
        if not messagebox.askyesno(
                "winapp2.ini",
                "Download the latest winapp2.ini - community cleaning rules for thousands of programs - "
                f"from github.com/MoscaDotTo/Winapp2?\n\nIt is saved to {winapp2.WINAPP2_FILE}. "
                "Rules for programs on this PC appear unticked in 'winapp2' groups.", parent=self):
            return
        self.working = True
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
        self.working = False
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
        if self.working:
            return
        selected = [t for t in self.targets if self.checked.get(t.id) and self._usable(t)]
        if not selected:
            messagebox.showinfo("PC Cleaner", "Tick at least one item first.", parent=self)
            return

        if mode == "scan":
            self._run(mode, selected)
            return

        running = winutil.running_processes()
        blocked = [t for t in selected if any(p in running for p in t.processes)]
        to_close = sorted({p for t in blocked for p in t.processes if p in running})
        close_apps = False
        if blocked:
            answer = messagebox.askyesnocancel(
                "Close programs?",
                f"These programs are open, so their data can't be cleaned:\n\n{_app_list(to_close)}\n\n"
                "Yes  -  close them now and clean everything (save your work in them first)\n"
                "No  -  leave them open and skip their items\n"
                "Cancel  -  go back", icon="question", parent=self)
            if answer is None:
                return
            close_apps = answer
            if not close_apps:
                selected = [t for t in selected if t not in blocked]
                if not selected:
                    return
        if not messagebox.askyesno(
                "Confirm cleaning",
                f"Permanently delete the data for {len(selected)} selected item(s)?\n\n"
                "This cannot be undone.", icon="warning", parent=self):
            return
        if close_apps:
            self._close_then_clean(selected, to_close)
        else:
            self._run("clean", selected)

    def _set_working(self, working: bool):
        self.working = working
        for button in (self.scan_btn, self.clean_btn):
            button.state(["disabled"] if working else ["!disabled"])

    def _close_then_clean(self, selected: list[Target], exes: list[str]):
        """Close the programs gracefully in the background, then clean."""
        self._set_working(True)
        self.hero_value.configure(text="Closing programs...", bootstyle="primary")
        self.hero_caption.configure(text=", ".join(app_name(p) for p in exes))
        self._log(f"Closing {', '.join(app_name(p) for p in exes)}...")

        def work():
            remaining = winutil.close_processes(exes)
            self.call_soon(self._after_close, selected, exes, remaining, False)

        threading.Thread(target=work, daemon=True).start()

    def _after_close(self, selected: list[Target], exes: list[str], remaining: set[str], forced: bool):
        if remaining and not forced:
            if messagebox.askyesno(
                    "Force close?",
                    f"These programs did not close:\n\n{_app_list(remaining)}\n\n"
                    "They may be asking whether to save changes, or still running in the background "
                    "(for example a browser with background apps or startup boost).\n\n"
                    "Yes  -  force close them (unsaved work in them is lost)\n"
                    "No  -  leave them running and skip their items", icon="warning", parent=self):
                self._log(f"Force closing {', '.join(app_name(p) for p in sorted(remaining))}...")

                def work():
                    still = winutil.kill_processes(remaining)
                    self.call_soon(self._after_close, selected, exes, still, True)

                threading.Thread(target=work, daemon=True).start()
                return
        closed = [p for p in exes if p not in remaining]
        if closed:
            self._log(f"Closed: {', '.join(app_name(p) for p in closed)}")
        if remaining:
            self._log(f"Still running, their items are skipped: {', '.join(app_name(p) for p in sorted(remaining))}")
            selected = [t for t in selected if not any(p in remaining for p in t.processes)]
        self._set_working(False)
        if selected:
            self._run("clean", selected)
        else:
            self.hero_value.configure(text="Ready", bootstyle="primary")
            self.hero_caption.configure(text="Nothing was cleaned.")

    def _run(self, mode: str, selected: list[Target]):
        self._set_working(True)
        self.mode = mode
        self.results.delete(*self.results.get_children())
        self.run_rows: list[tuple[str, Stats]] = []
        self.progress.configure(maximum=len(selected), value=0)
        self.hero_value.configure(text="Analyzing..." if mode == "scan" else "Cleaning...", bootstyle="primary")
        self.hero_caption.configure(text=f"{len(selected)} item(s)")
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
                kind, a, b = self.events.get_nowait()
                if kind == "call":
                    a(*b)  # function queued from a worker thread, run on the UI thread
                else:
                    self._handle(kind, a, b)
        except queue.Empty:
            pass
        self._drain_job = self.after(80, self._drain_events)

    def call_soon(self, func, *args):
        """Thread-safe: run func(*args) on the UI thread."""
        self.events.put(("call", func, args))

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
            if status:
                tag = "note"
            elif not b.found:
                status, tag = "Nothing found", "empty"
            else:
                status, tag = ("OK", "ok") if self.mode == "scan" else ("Cleaned", "cleaned")
            self.results.insert("", "end", tags=(tag,),
                                values=(f"{a.group}: {a.name}", ", ".join(found) or "-",
                                        human_size(b.bytes) if b.bytes else "-", f"    {status}"))
            for msg in b.messages:
                self._log(f"{a.group} / {a.name}: {msg}")
        elif kind == "done":
            mode, total = a, b
            self._set_working(False)
            self.status.configure(text="Ready")
            self.progress.configure(value=self.progress.cget("maximum"))
            counts = f"{total.files:,} files \u00b7 {total.entries:,} history entries"
            if mode == "scan":
                self.hero_value.configure(text=human_size(total.bytes), bootstyle="primary")
                self.hero_caption.configure(text=f"can be freed  \u00b7  {counts}")
            else:
                self.hero_value.configure(text=f"{human_size(total.bytes)} freed", bootstyle="success")
                self.hero_caption.configure(text=counts)
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

    def destroy(self):
        try:
            self.after_cancel(self._drain_job)
        except (AttributeError, tk.TclError):
            pass
        super().destroy()

    def _on_close(self):
        self._save_selection()
        self.destroy()


class ScheduleDialog(tb.Toplevel):
    """Create, change or remove the scheduled cleaning task."""

    def __init__(self, app: App):
        super().__init__(title="Scheduled cleaning", resizable=(False, False), transient=app, iconphoto=None)
        self.app = app

        saved = settings.load().get("schedule") or {}
        self.items = app.ticked_targets()

        frame = tb.Frame(self, padding=px(20))
        frame.pack(fill="both", expand=True)
        tb.Label(frame, text="Scheduled cleaning", font=(FONT, 14, "bold")).grid(
            row=0, column=0, columnspan=4, sticky="w")
        self.status = tb.Label(frame, text="Checking current schedule...", bootstyle="secondary")
        self.status.grid(row=1, column=0, columnspan=4, sticky="w", pady=(2, 14))

        tb.Label(frame, text="Run", font=(FONT, 10, "bold")).grid(row=2, column=0, sticky="nw", padx=(0, 14))
        self.frequency = tk.StringVar(value=saved.get("frequency", "weekly"))
        tb.Radiobutton(frame, text="Every day", value="daily", variable=self.frequency,
                       command=self._sync).grid(row=2, column=1, sticky="w")
        tb.Radiobutton(frame, text="Every week on", value="weekly", variable=self.frequency,
                       command=self._sync).grid(row=3, column=1, sticky="w", pady=(6, 0))
        self.day = tk.StringVar(value=saved.get("day", "Sunday"))
        self.day_box = tb.Combobox(frame, textvariable=self.day, values=scheduler.DAYS, state="readonly", width=12)
        self.day_box.grid(row=3, column=2, sticky="w", padx=6, pady=(6, 0))

        hour, minute = (saved.get("time") or "20:00").split(":")
        tb.Label(frame, text="At", font=(FONT, 10, "bold")).grid(row=4, column=0, sticky="w", pady=(12, 0))
        at = tb.Frame(frame)
        at.grid(row=4, column=1, columnspan=3, sticky="w", pady=(12, 0))
        self.hour = tb.Spinbox(at, from_=0, to=23, width=4, format="%02.0f", wrap=True)
        self.hour.set(hour)
        self.hour.pack(side="left")
        tb.Label(at, text=" : ").pack(side="left")
        self.minute = tb.Spinbox(at, from_=0, to=59, width=4, format="%02.0f", wrap=True, increment=5)
        self.minute.set(minute)
        self.minute.pack(side="left")
        tb.Label(at, text="If the PC is off, it runs at the next start-up.",
                 bootstyle="secondary").pack(side="left", padx=12)

        tb.Label(frame, text=f"Cleans the {len(self.items)} item(s) ticked now", font=(FONT, 10, "bold")).grid(
            row=5, column=0, columnspan=4, sticky="w", pady=(16, 6))
        box_frame = tb.Frame(frame)
        box_frame.grid(row=6, column=0, columnspan=4, sticky="we")
        box = _scrolled(box_frame, lambda p: tb.Listbox(p, height=8, width=64, activestyle="none", relief="flat"))
        for t in self.items:
            box.insert("end", f"  {t.group}: {t.name}")

        notes = ["Programs that are open at that time are skipped. Each run saves a report (Reports)."]
        if not app.is_admin:
            notes.append("Items marked (admin) are not included - run PC Cleaner as administrator "
                         "before saving the schedule to include them.")
        tb.Label(frame, text="\n".join(notes), wraplength=px(480), justify="left", bootstyle="secondary").grid(
            row=7, column=0, columnspan=4, sticky="w", pady=(10, 0))

        buttons = tb.Frame(frame)
        buttons.grid(row=8, column=0, columnspan=4, sticky="e", pady=(18, 0))
        tb.Button(buttons, text="Close", bootstyle="secondary-outline", command=self.destroy).pack(side="right")
        tb.Button(buttons, text="Turn off", icon="calendar-x", bootstyle="danger-outline",
                  command=self._remove).pack(side="right", padx=8)
        tb.Button(buttons, text="Save schedule", icon="check-lg", bootstyle="primary",
                  command=self._save).pack(side="right")

        self._sync()
        self._refresh_status()
        _match_title_bar(self)
        self.grab_set()  # after the title bar fix, which briefly hides the window

    def _sync(self):
        self.day_box.configure(state="readonly" if self.frequency.get() == "weekly" else "disabled")

    def _refresh_status(self):
        def work():
            saved = settings.load().get("schedule")
            nxt = scheduler.next_run()
            text = (f"Current: {scheduler.describe(saved)} \u00b7 next run {nxt}" if saved and nxt
                    else "No schedule is set.")
            self.app.call_soon(self._set_status, text)
        threading.Thread(target=work, daemon=True).start()

    def _set_status(self, text: str):
        if self.winfo_exists():
            self.status.configure(text=text)

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
        self._refresh_status()

    def _remove(self):
        try:
            scheduler.remove()
        except Exception as exc:
            messagebox.showerror("Scheduled cleaning", f"Could not remove the task:\n{exc}", parent=self)
            return
        self.app._log("Schedule turned off.")
        self.status.configure(text="No schedule is set.")


class LargeFilesWindow(tb.Toplevel):
    """Find the biggest files in a folder and send chosen ones to the Recycle Bin."""

    COLUMNS = (("name", "Name", 280, "w"), ("size", "Size", 90, "e"),
               ("modified", "    Modified", 150, "w"), ("folder", "Folder", 400, "w"))

    def __init__(self, app: App):
        super().__init__(title="Large File Finder", iconphoto=None)
        _geometry(self, (1000, 640), (760, 440))
        self.app = app
        self.files: dict[str, largefiles.FileInfo] = {}
        self.cancel = threading.Event()
        self.events: queue.Queue = queue.Queue()
        self.sort_key, self.sort_desc = "size", True

        frame = tb.Frame(self, padding=px(18))
        frame.pack(fill="both", expand=True)
        tb.Label(frame, text="Large File Finder", font=(FONT, 14, "bold")).pack(anchor="w")
        tb.Label(frame, text="Find big files you no longer need. Files go to the Recycle Bin, so they can be "
                             "restored.", bootstyle="secondary").pack(anchor="w", pady=(2, 12))

        opts = tb.Frame(frame)
        opts.pack(fill="x")
        tb.Label(opts, text="Folder", font=(FONT, 10, "bold")).pack(side="left", padx=(0, 8))
        self.folder = tk.StringVar(value=os.environ.get("USERPROFILE", "C:\\"))
        tb.Button(opts, text="Browse", icon="folder2-open", bootstyle="secondary-outline",
                  command=self._browse).pack(side="right")
        tb.Entry(opts, textvariable=self.folder).pack(side="left", fill="x", expand=True, padx=(0, 8))

        filters = tb.Frame(frame)
        filters.pack(fill="x", pady=(10, 0))
        tb.Label(filters, text="At least").pack(side="left")
        self.min_mb = tb.Spinbox(filters, values=(10, 50, 100, 250, 500, 1000, 2000, 5000), width=6)
        self.min_mb.set(100)
        self.min_mb.pack(side="left", padx=6)
        tb.Label(filters, text="MB, not changed for").pack(side="left")
        self.min_days = tb.Spinbox(filters, values=(0, 30, 90, 180, 365, 730), width=5)
        self.min_days.set(0)
        self.min_days.pack(side="left", padx=6)
        tb.Label(filters, text="days (0 = any)").pack(side="left")
        self.skip_system = tk.BooleanVar(value=True)
        tb.Checkbutton(filters, text="Skip Windows and program folders", variable=self.skip_system,
                       bootstyle="round-toggle").pack(side="left", padx=18)
        self.find_btn = tb.Button(filters, text="Find", icon="search", bootstyle="primary", command=self._find)
        self.find_btn.pack(side="right")
        self.stop_btn = tb.Button(filters, text="Stop", icon="stop-circle", bootstyle="secondary-outline",
                                  command=self.cancel.set, state="disabled")
        self.stop_btn.pack(side="right", padx=8)

        table = tb.Frame(frame)
        table.pack(fill="both", expand=True, pady=12)
        self.tree = _scrolled(table, lambda p: tb.Treeview(p, columns=[c[0] for c in self.COLUMNS],
                                                           show="headings", selectmode="extended"))
        for col, title, width, anchor in self.COLUMNS:
            self.tree.heading(col, text=title, anchor=anchor, command=lambda c=col: self._sort(c))
            self.tree.column(col, width=px(width), anchor=anchor, stretch=col in ("name", "folder"))
        self.tree.bind("<Double-1>", lambda e: self._show())
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._update_selection())

        bottom = tb.Frame(frame)
        bottom.pack(fill="x")
        self.status = tb.Label(bottom, text="Choose a folder and press Find. Nothing is deleted without asking.",
                               bootstyle="secondary")
        self.status.pack(side="left")
        self.recycle_btn = tb.Button(bottom, text="Send to Recycle Bin", icon="trash3", bootstyle="danger",
                                     command=self._recycle, state="disabled")
        self.recycle_btn.pack(side="right")
        tb.Button(bottom, text="Show in Explorer", icon="folder2-open", bootstyle="secondary-outline",
                  command=self._show).pack(side="right", padx=8)

        self._apply_colors()
        app.theme_listeners.append(self._apply_colors)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(100, self._drain)

    def _apply_colors(self):
        self.tree.tag_configure("app", foreground=_warning_text())
        _match_title_bar(self)

    def _browse(self):
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
            modified = "    " + time.strftime("%Y-%m-%d %H:%M", time.localtime(f.modified))
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
        if self._apply_colors in self.app.theme_listeners:
            self.app.theme_listeners.remove(self._apply_colors)
        self.destroy()


def run():
    App().mainloop()
