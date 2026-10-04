"""Desktop applications and developer tool caches."""
from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path

from .. import regutil
from ..core import Custom, Files, Target
from ..fsutil import children, existing
from ..stats import Stats
from .browsers import CR_CACHE_DIRS, CR_SHARED_CACHE_DIRS

LOCAL = os.environ.get("LOCALAPPDATA", "")
ROAMING = os.environ.get("APPDATA", "")

# ---- Microsoft Office: "File MRU" / "Place MRU" keys under every app and account ----

OFFICE = r"Software\Microsoft\Office"
_OFFICE_MRU = {"file mru", "place mru"}
_OFFICE_PROCS = ("winword.exe", "excel.exe", "powerpnt.exe", "msaccess.exe", "mspub.exe",
                 "visio.exe", "onenote.exe")


def _office_mru_keys() -> list[str]:
    versions = [v for v in regutil.subkeys(OFFICE) if re.fullmatch(r"\d+\.\d+", v)]
    return [k for v in versions for k in regutil.find_keys(f"{OFFICE}\\{v}", _OFFICE_MRU, max_depth=4)]


def _office_scan() -> Stats:
    names = [n for k in _office_mru_keys() for n in regutil.value_names(k)]
    return Stats(entries=sum(1 for n in names if re.fullmatch(r"Item \d+", n)))


def _office_clean() -> Stats:
    stats = _office_scan()
    for key in _office_mru_keys():
        regutil.clear_values(key)
    return stats


# ---- Notepad++: <History> and <FindHistory> in config.xml ------------------------

NPP_CONFIG = os.path.join(ROAMING, "Notepad++", "config.xml")
_NPP_BLOCK = re.compile(r"(<(History|FindHistory)\b[^>]*(?<!/)>)(.*?)(</\2>)", re.S)
_NPP_ITEM = re.compile(r"[ \t]*<(?:File|Path|Filter|Find|Replace)\b[^>]*/>[ \t]*\r?\n?")


def _read_exact(path: str) -> str:
    """Read text keeping the original line endings and any undecodable bytes."""
    with open(path, encoding="utf-8", errors="surrogateescape", newline="") as f:
        return f.read()


def npp_scan(path: str = NPP_CONFIG) -> Stats:
    if not os.path.isfile(path):
        return Stats()
    text = _read_exact(path)
    return Stats(entries=sum(len(_NPP_ITEM.findall(m.group(3))) for m in _NPP_BLOCK.finditer(text)))


def npp_clean(path: str = NPP_CONFIG) -> Stats:
    stats = npp_scan(path)
    if stats.entries:
        text = _read_exact(path)
        text = _NPP_BLOCK.sub(lambda m: m.group(1) + _NPP_ITEM.sub("", m.group(3)) + m.group(4), text)
        with open(path, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
            f.write(text)
    return stats


# ---- VS Code family: caches + "File > Open Recent" ----------------------------------

VSCODE_EDITORS = [  # (id, display name, %APPDATA% folder, process)
    ("vscode", "Visual Studio Code", "Code", "code.exe"),
    ("vscode_insiders", "VS Code Insiders", "Code - Insiders", "code - insiders.exe"),
    ("vscodium", "VSCodium", "VSCodium", "vscodium.exe"),
    ("cursor", "Cursor", "Cursor", "cursor.exe"),
]
VSCODE_CACHE_DIRS = CR_CACHE_DIRS + ["CachedData", "CachedExtensionVSIXs", "logs", r"Crashpad\reports"]
VSCODE_RECENT_KEY = "history.recentlyOpenedPathsList"


def _vscode_recent_count(db: str) -> int:
    con = sqlite3.connect(Path(db).as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        row = con.execute("SELECT value FROM ItemTable WHERE key = ?", (VSCODE_RECENT_KEY,)).fetchone()
    finally:
        con.close()
    return len(json.loads(row[0]).get("entries", [])) if row else 0


def _vscode_recent_clear(db: str) -> int:
    count = _vscode_recent_count(db)
    if count:
        con = sqlite3.connect(db, timeout=5)
        try:
            with con:
                con.execute("UPDATE ItemTable SET value = ? WHERE key = ?",
                            (json.dumps({"entries": []}), VSCODE_RECENT_KEY))
        finally:
            con.close()
    return count


def _vscode_targets(key, name, folder, process):
    root = os.path.join(ROAMING, folder)
    if not os.path.isdir(root):
        return
    db = os.path.join(root, r"User\globalStorage\state.vscdb")

    def recent(fn):
        def run() -> Stats:
            return Stats(entries=fn(db)) if os.path.isfile(db) else Stats()
        return run

    yield Target(f"{key}.cache", name, "Cache & logs",
                 "Web/GPU caches, compiled code cache, downloaded extension packages and logs. "
                 "Settings, extensions and unsaved files are not touched.",
                 [Files(lambda: [p for d in existing(root, VSCODE_CACHE_DIRS + CR_SHARED_CACHE_DIRS)
                                 for p in children(d)])],
                 processes=(process,))
    yield Target(f"{key}.recent", name, "Recently opened files & folders",
                 "The File > Open Recent list.",
                 [Custom(recent(_vscode_recent_count), recent(_vscode_recent_clear))],
                 processes=(process,))


# ---- developer package caches ------------------------------------------------

DEV_CACHES = [  # (id, name, folder, description)
    ("dev.npm", "npm cache", os.path.join(LOCAL, "npm-cache"),
     "Downloaded npm packages and logs; npm downloads them again when needed."),
    ("dev.yarn", "Yarn cache", os.path.join(LOCAL, r"Yarn\Cache"), "Downloaded Yarn packages."),
    ("dev.pip", "pip cache", os.path.join(LOCAL, r"pip\Cache"), "Downloaded Python packages and wheels."),
    ("dev.nuget", "NuGet HTTP cache", os.path.join(LOCAL, r"NuGet\v3-cache"), "Cached NuGet feed responses."),
]


def targets():
    if regutil.key_exists(OFFICE) and _office_mru_keys():
        yield Target("office.recent", "Microsoft Office", "Recent documents & places",
                     "Recent files and folders in Word, Excel, PowerPoint and other Office apps. "
                     "Files stored in OneDrive may reappear because Office syncs them from your account.",
                     [Custom(_office_scan, _office_clean)], processes=_OFFICE_PROCS)

    if os.path.isfile(NPP_CONFIG):
        yield Target("notepadpp.history", "Notepad++", "Recent files & search history",
                     "Recent files list and Find / Replace / Find-in-files history. "
                     "Open tabs and unsaved work are not touched.",
                     [Custom(npp_scan, npp_clean)], processes=("notepad++.exe",))

    for editor in VSCODE_EDITORS:
        yield from _vscode_targets(*editor)

    photos = os.path.join(LOCAL, r"Packages\Microsoft.Windows.Photos_8wekyb3d8bbwe\LocalState\EBWebView")
    if os.path.isdir(photos):
        yield Target("photos.cache", "Windows Photos", "Cache",
                     "Web content and GPU caches of the Photos app.",
                     [Files(lambda: [p for d in existing(os.path.join(photos, "Default"), CR_CACHE_DIRS)
                                     + existing(photos, CR_SHARED_CACHE_DIRS) for p in children(d)])],
                     processes=("photos.exe", "microsoft.photos.exe"))

    for key, name, folder, desc in DEV_CACHES:
        if os.path.isdir(folder):
            yield Target(key, "Developer tools", name, desc,
                         [Files(lambda folder=folder: children(folder))])
