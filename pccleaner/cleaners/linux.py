"""Linux cleaning catalog (RudramOS and other GNOME desktops).

The same rules as on Windows: safe items ticked by default, disruptive ones (Trash, thumbnails,
cookies, shell history) unticked, protected folders refused, links removed but never followed,
and caches keep anything changed in the last hour. Browsers are covered both as normal installs
and as Flatpaks (~/.var/app/<id>), which is how RudramOS ships Firefox.

Paths are worked out each time they're used, so HOME / XDG_* can change (tests do).
System maintenance items run through the pccleaner-system helper with admin approval (pkexec).
"""
from __future__ import annotations

import os
import re
import subprocess

from ..core import Custom, Files, Target
from ..fsutil import children, existing, glob_in, measure
from ..stats import Stats
from . import browsers, media

HELPER = "/usr/libexec/pccleaner/pccleaner-system"


def home(*parts: str) -> str:
    return os.path.join(os.path.expanduser("~"), *parts)


def _xdg(variable: str, fallback: str, *parts: str) -> str:
    return os.path.join(os.environ.get(variable) or home(fallback), *parts)


def cache(*parts: str) -> str:
    return _xdg("XDG_CACHE_HOME", ".cache", *parts)


def config(*parts: str) -> str:
    return _xdg("XDG_CONFIG_HOME", ".config", *parts)


def data(*parts: str) -> str:
    return _xdg("XDG_DATA_HOME", ".local/share", *parts)


def state(*parts: str) -> str:
    return _xdg("XDG_STATE_HOME", ".local/state", *parts)


def flatpak(app_id: str, *parts: str) -> str:
    return home(".var", "app", app_id, *parts)


# ---- System --------------------------------------------------------------------------------

SYSTEM = "System"
# ~/.cache folders that have their own item below (so they aren't counted twice)
_CACHE_OWN_ITEM = {"thumbnails", "mozilla", "google-chrome", "chromium", "BraveSoftware", "microsoft-edge",
                   "vivaldi", "mesa_shader_cache", "mesa_shader_cache_db", "nvidia", "pip", "yarn",
                   "go-build", "vlc"}
# Flatpak apps whose caches have their own item
_FLATPAK_OWN_ITEM = {"org.mozilla.firefox", "com.google.Chrome", "org.chromium.Chromium", "com.brave.Browser",
                     "com.microsoft.Edge", "com.vivaldi.Vivaldi"}


def _app_caches() -> list[str]:
    return [p for p in children(cache()) if os.path.basename(p) not in _CACHE_OWN_ITEM]


def _flatpak_caches() -> list[str]:
    return [p for app in children(home(".var", "app")) if os.path.basename(app) not in _FLATPAK_OWN_ITEM
            for p in children(os.path.join(app, "cache"))]


def _shader_caches() -> list[str]:
    dirs = glob_in(cache(), "mesa_shader_cache*") + [cache("nvidia", "GLCache"), home(".nv", "ComputeCache")]
    return [p for d in dirs for p in children(d)]


def _trash() -> list[str]:
    return [p for d in ("files", "info", "expunged") for p in children(data("Trash", d))]


def _system_targets():
    yield Target("linux.app_cache", SYSTEM, "Application caches",
                 "Temporary files apps keep in ~/.cache; they are rebuilt when needed. "
                 "Files changed in the last hour are kept.", [Files(_app_caches, min_age_hours=1)])
    if os.path.isdir(home(".var", "app")):
        yield Target("linux.flatpak_cache", SYSTEM, "Flatpak app caches",
                     "The same for apps from the App Store (Flatpak). Files changed in the last hour are kept.",
                     [Files(_flatpak_caches, min_age_hours=1)])
    yield Target("linux.shader_cache", SYSTEM, "Graphics shader caches",
                 "Compiled shaders from the graphics driver (Mesa, NVIDIA); rebuilt on next use. "
                 "Games may stutter briefly the first time afterwards.", [Files(_shader_caches)])
    yield Target("linux.thumbnails", SYSTEM, "Thumbnail cache",
                 "Picture previews in Files. Folders with many pictures load slower once while they're redrawn.",
                 [Files(lambda: children(cache("thumbnails")))], default=False)
    yield Target("linux.trash", SYSTEM, "Empty Trash", "Permanently deletes everything in the Trash.",
                 [Files(_trash)], default=False)


# ---- Desktop history ---------------------------------------------------------------------------

DESKTOP = "Desktop history"


def _desktop_targets():
    yield Target("linux.recent_files", DESKTOP, "Recently used files",
                 "The 'Recent' list in Files and in Open/Save dialogs.",
                 [Files(lambda: existing(data(), ["recently-used.xbel", "recently-used.xbel.bak"]))])
    yield Target("linux.shell_history", DESKTOP, "Terminal command history",
                 "Commands you typed in the terminal (bash, zsh, fish) and Python's interactive history.",
                 [Files(lambda: existing(home(), [".bash_history", ".zsh_history", ".python_history"])
                        + existing(data("fish"), ["fish_history"]))],
                 default=False, warning="Terminal command history: the Up arrow won't find old commands afterwards.")


# ---- Browsers (normal and Flatpak) ---------------------------------------------------------------

# key, name, folder under ~/.config, Flatpak id, process names
_CHROMIUM = [
    ("chrome", "Google Chrome", "google-chrome", "com.google.Chrome", ("chrome",)),
    ("chromium", "Chromium", "chromium", "org.chromium.Chromium", ("chromium", "chromium-browser", "chromium-browse")),
    ("brave", "Brave", os.path.join("BraveSoftware", "Brave-Browser"), "com.brave.Browser", ("brave", "brave-browser")),
    ("edge", "Microsoft Edge", "microsoft-edge", "com.microsoft.Edge", ("msedge",)),
    ("vivaldi", "Vivaldi", "vivaldi", "com.vivaldi.Vivaldi", ("vivaldi-bin", "vivaldi")),
]


def chromium_browsers() -> list[browsers.Chromium]:
    result = []
    for key, name, folder, app_id, procs in _CHROMIUM:
        result.append(browsers.Chromium(key, name, config(folder), procs[0], cache_root=cache(folder),
                                        other_processes=procs[1:]))
        result.append(browsers.Chromium(f"{key}_flatpak", f"{name} (Flatpak)", flatpak(app_id, "config", folder),
                                        procs[0], cache_root=flatpak(app_id, "cache", folder),
                                        other_processes=procs[1:]))
    return result


def firefox_profiles() -> list[tuple[str, str]]:
    """Every Firefox profile: normal install (old and new XDG folders) and the Flatpak."""
    app = "org.mozilla.firefox"
    roots = [
        (home(".mozilla", "firefox"), cache("mozilla", "firefox")),
        (config("mozilla", "firefox"), cache("mozilla", "firefox")),
        (flatpak(app, ".mozilla", "firefox"), flatpak(app, "cache", "mozilla", "firefox")),
        (flatpak(app, "config", "mozilla", "firefox"), flatpak(app, "cache", "mozilla", "firefox")),
    ]
    seen, result = set(), []
    for root, cache_root in roots:
        for prof, prof_cache in browsers.firefox_profiles(root, cache_root):
            if os.path.realpath(prof) not in seen:
                seen.add(os.path.realpath(prof))
                result.append((prof, prof_cache))
    return result


def _browser_targets():
    for b in chromium_browsers():
        if b.profiles():
            yield from browsers._chromium_targets(b)
    if firefox_profiles():
        yield from browsers.firefox_targets(firefox_profiles, procs=("firefox", "firefox-bin"))


# ---- Media players -------------------------------------------------------------------------------

MEDIA = "Media players"


def _vlc_inis() -> list[str]:
    return [p for p in (config("vlc", "vlc-qt-interface.ini"),
                        flatpak("org.videolan.VLC", "config", "vlc", "vlc-qt-interface.ini")) if os.path.isfile(p)]


def _mpv_watch_later() -> list[str]:
    dirs = [state("mpv", "watch_later"), config("mpv", "watch_later"),
            flatpak("io.mpv.Mpv", ".local", "state", "mpv", "watch_later"), flatpak("io.mpv.Mpv", "config", "mpv", "watch_later")]
    return [p for d in dirs for p in children(d)]


def _media_targets():
    if _vlc_inis():
        def scan() -> Stats:
            total = Stats()
            for ini in _vlc_inis():
                total.merge(media.vlc_scan(ini))
            return total

        def clean() -> Stats:
            total = Stats()
            for ini in _vlc_inis():
                total.merge(media.vlc_clean(ini))
            return total

        yield Target("media.vlc", MEDIA, "VLC: recently played",
                     "'Media > Open Recent Media' list, remembered resume positions and network stream history.",
                     [Custom(scan, clean)], processes=("vlc",))
    if _mpv_watch_later():
        yield Target("media.mpv", MEDIA, "mpv: resume positions",
                     "Where each video was stopped, so it doesn't continue from there next time.",
                     [Files(_mpv_watch_later)], processes=("mpv", "celluloid"))


# ---- Apps and developer tools ------------------------------------------------------------------------

APPS = "Apps"
DEV = "Developer tools"
_EDITOR_CACHES = ["Cache", "CachedData", "Code Cache", "GPUCache", "CachedExtensionVSIXs", "DawnCache",
                  "DawnGraphiteCache", "logs"]


def _editor_targets():
    editors = [
        ("vscode", "Visual Studio Code", [config("Code"), flatpak("com.visualstudio.code", "config", "Code")], ("code",)),
        ("vscodium", "VSCodium", [config("VSCodium"), flatpak("com.vscodium.codium", "config", "VSCodium")], ("codium",)),
    ]
    for key, name, roots, procs in editors:
        found = [r for r in roots if os.path.isdir(r)]
        if found:
            yield Target(f"{key}.cache", APPS, f"{name}: caches",
                         "Cached extensions, compiled code and logs; rebuilt on next start.",
                         [Files(lambda found=found: [p for r in found for d in existing(r, _EDITOR_CACHES)
                                                     for p in children(d)])], processes=procs)


def _dev_targets():
    tools = [
        ("dev.npm", "npm cache", lambda: children(home(".npm", "_cacache"))),
        ("dev.yarn", "Yarn cache", lambda: children(cache("yarn"))),
        ("dev.pip", "pip cache", lambda: children(cache("pip"))),
        ("dev.cargo", "Cargo download cache", lambda: children(home(".cargo", "registry", "cache"))),
        ("dev.go", "Go build cache", lambda: children(cache("go-build"))),
        ("dev.nuget", "NuGet cache", lambda: children(data("NuGet", "v3-cache")) + children(data("NuGet", "http-cache"))),
    ]
    for tid, name, paths in tools:
        if paths():
            yield Target(tid, DEV, name, "Downloaded packages kept for reuse; downloaded again when needed.",
                         [Files(paths)])


# ---- System maintenance (admin approval each time) -----------------------------------------------

MAINTENANCE = "System maintenance"


def _helper(action: str) -> subprocess.CompletedProcess:
    return subprocess.run(["pkexec", HELPER, action], capture_output=True, text=True, errors="replace", timeout=900)


def _approved(action: str, done) -> Stats:
    """Runs a helper action; `done(output)` turns its output into Stats."""
    try:
        result = _helper(action)
    except (OSError, subprocess.SubprocessError) as exc:
        stats = Stats()
        stats.note(f"Couldn't run the system helper: {exc}")
        return stats
    if result.returncode in (126, 127):
        stats = Stats()
        stats.note("Skipped: administrator approval was cancelled")
        return stats
    if result.returncode != 0:
        stats = Stats()
        stats.note((result.stderr or result.stdout).strip().splitlines()[-1] if (result.stderr or result.stdout).strip()
                   else f"System helper failed ({result.returncode})")
        return stats
    return done(result.stdout)


_SIZE = re.compile(r"([\d.]+)\s*([KMGT]?)i?B?\b", re.I)


def parse_size(text: str) -> int:
    """'1.2G' / '512.0M' / '24K' (journalctl's sizes) -> bytes."""
    m = _SIZE.search(text)
    if not m:
        return 0
    factor = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}[m.group(2).upper()]
    return int(float(m.group(1)) * factor)


def journal_usage() -> int:
    try:
        out = subprocess.run(["journalctl", "--disk-usage"], capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return 0
    m = re.search(r"take up ([\d.]+\s*[KMGT]?)", out)
    return parse_size(m.group(1)) if m else 0


def _journal_scan() -> Stats:
    stats = Stats(bytes=journal_usage())
    stats.note("The last 7 days are kept")
    return stats


def _journal_clean() -> Stats:
    before = journal_usage()
    return _approved("journal", lambda _out: Stats(bytes=max(0, before - journal_usage())))


COREDUMPS = "/var/lib/systemd/coredump"


def _coredump_scan() -> Stats:
    stats = Stats()
    for p in children(COREDUMPS):
        measure(p, stats)
    return stats


def _coredump_clean() -> Stats:
    before = _coredump_scan()
    return _approved("coredumps", lambda _out: Stats(files=before.files, bytes=before.bytes))


def _flatpak_unused_clean() -> Stats:
    def done(out: str) -> Stats:
        removed = [line for line in out.splitlines() if re.search(r"\bUninstall(ing|ed)\b", line, re.I)]
        stats = Stats(entries=len(removed))
        if not removed:
            stats.note("Nothing unused")
        return stats

    return _approved("flatpak-unused", done)


def _maintenance_targets():
    if not os.path.exists(HELPER):
        return  # only where the system helper is installed (RudramOS)
    yield Target("linux.journal", MAINTENANCE, "Old system logs",
                 "System log entries older than 7 days.", [Custom(_journal_scan, _journal_clean)],
                 default=False, approval=True)
    yield Target("linux.coredumps", MAINTENANCE, "Crash dumps",
                 "Memory dumps saved when a program crashed. Only useful for reporting that crash.",
                 [Custom(_coredump_scan, _coredump_clean)], default=False, approval=True)
    yield Target("linux.flatpak_unused", MAINTENANCE, "Unused app runtimes",
                 "Flatpak runtimes no installed app needs any more (the size shows after cleaning).",
                 [Custom(Stats, _flatpak_unused_clean)], default=False, approval=True)


def targets():
    for group in (_system_targets, _desktop_targets, _browser_targets, _media_targets, _editor_targets,
                  _dev_targets, _maintenance_targets):
        yield from group()
