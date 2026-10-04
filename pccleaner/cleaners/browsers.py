"""Web browsers: Chromium family (Chrome, Edge, Brave, Opera, Vivaldi) and Firefox."""
from __future__ import annotations

import configparser
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from ..core import Custom, Files, Target
from ..fsutil import children, existing
from ..stats import Stats

LOCAL = os.environ.get("LOCALAPPDATA", "")
ROAMING = os.environ.get("APPDATA", "")

# ---- Chromium ---------------------------------------------------------------

CR_HISTORY = ["History", "History-journal", "Visited Links", "Top Sites", "Top Sites-journal",
              "Shortcuts", "Shortcuts-journal", "Network Action Predictor",
              "Network Action Predictor-journal", "Favicons", "Favicons-journal"]
CR_CACHE_DIRS = ["Cache", "Code Cache", "GPUCache", "DawnCache", "DawnGraphiteCache",
                 "DawnWebGPUCache", r"Service Worker\CacheStorage", r"Service Worker\ScriptCache"]
CR_SHARED_CACHE_DIRS = ["ShaderCache", "GrShaderCache", "GraphiteDawnCache"]
CR_COOKIES = [r"Network\Cookies", r"Network\Cookies-journal", "Cookies", "Cookies-journal"]
CR_SESSIONS = ["Sessions", "Current Session", "Current Tabs", "Last Session", "Last Tabs"]


@dataclass
class Chromium:
    key: str
    name: str
    data_dir: str
    process: str
    local_dir: str | None = None  # Opera: data_dir is the profile, caches live here

    def profiles(self) -> list[tuple[str, str]]:
        """(profile dir, cache dir) pairs."""
        if self.local_dir is not None:
            return [(self.data_dir, self.local_dir)] if os.path.isdir(self.data_dir) else []
        result = []
        for path in children(self.data_dir):
            name = os.path.basename(path)
            if os.path.isdir(path) and (name in ("Default", "Guest Profile") or name.startswith("Profile ")):
                result.append((path, path))
        return result

    @property
    def shared_root(self) -> str:
        return self.local_dir or self.data_dir


CHROMIUM = [
    Chromium("chrome", "Google Chrome", os.path.join(LOCAL, r"Google\Chrome\User Data"), "chrome.exe"),
    Chromium("edge", "Microsoft Edge", os.path.join(LOCAL, r"Microsoft\Edge\User Data"), "msedge.exe"),
    Chromium("brave", "Brave", os.path.join(LOCAL, r"BraveSoftware\Brave-Browser\User Data"), "brave.exe"),
    Chromium("vivaldi", "Vivaldi", os.path.join(LOCAL, r"Vivaldi\User Data"), "vivaldi.exe"),
    Chromium("ulaa", "Zoho Ulaa", os.path.join(LOCAL, r"Zoho\Ulaa\User Data"), "ulaa.exe"),
    Chromium("chromium", "Chromium", os.path.join(LOCAL, r"Chromium\User Data"), "chrome.exe"),
    Chromium("opera", "Opera", os.path.join(ROAMING, r"Opera Software\Opera Stable"), "opera.exe",
             os.path.join(LOCAL, r"Opera Software\Opera Stable")),
    Chromium("opera_gx", "Opera GX", os.path.join(ROAMING, r"Opera Software\Opera GX Stable"), "opera.exe",
             os.path.join(LOCAL, r"Opera Software\Opera GX Stable")),
]


def _chromium_targets(b: Chromium):
    group, procs = b.name, (b.process,)
    close = f"{b.name} must be closed."

    def history():
        return [p for prof, _ in b.profiles() for p in existing(prof, CR_HISTORY)]

    def cache():
        paths = [p for _, cache_dir in b.profiles()
                 for d in existing(cache_dir, CR_CACHE_DIRS) for p in children(d)]
        return paths + [p for d in existing(b.shared_root, CR_SHARED_CACHE_DIRS) for p in children(d)]

    def cookies():
        return [p for prof, _ in b.profiles() for p in existing(prof, CR_COOKIES)]

    def sessions():
        return [p for prof, _ in b.profiles() for p in existing(prof, CR_SESSIONS)]

    yield Target(f"{b.key}.history", group, "Browsing history",
                 f"Visited pages, top sites, address-bar suggestions and favicons in every profile. {close}",
                 [Files(history)], processes=procs)
    yield Target(f"{b.key}.cache", group, "Cache",
                 "Cached web pages, images, scripts and GPU shaders.", [Files(cache)], processes=procs)
    yield Target(f"{b.key}.cookies", group, "Cookies",
                 "Signs you out of all websites.", [Files(cookies)], default=False, processes=procs)
    yield Target(f"{b.key}.sessions", group, "Saved tabs / session",
                 "Tabs restored on next start.", [Files(sessions)], default=False, processes=procs)


# ---- Firefox ----------------------------------------------------------------

def firefox_profiles() -> list[tuple[str, str]]:
    """(profile dir, cache dir) pairs from profiles.ini."""
    root = os.path.join(ROAMING, r"Mozilla\Firefox")
    ini = configparser.ConfigParser(interpolation=None)
    try:
        ini.read(os.path.join(root, "profiles.ini"), encoding="utf-8")
    except configparser.Error:
        return []
    result = []
    for name in ini.sections():
        sec = ini[name]
        if not name.lower().startswith("profile") or "path" not in sec:
            continue
        path = sec["path"].replace("/", os.sep)
        if sec.get("isrelative", "1") == "1":
            prof, cache = os.path.join(root, path), os.path.join(LOCAL, r"Mozilla\Firefox", path)
        else:
            prof = cache = path
        if os.path.isdir(prof):
            result.append((prof, cache))
    return result


def _places_statements(con: sqlite3.Connection) -> list[str]:
    """SQL that wipes history from places.sqlite while keeping bookmarks."""
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    cols = {r[1] for r in con.execute("PRAGMA table_info(moz_places)")}

    keep = ["id NOT IN (SELECT fk FROM moz_bookmarks WHERE fk IS NOT NULL)"]
    if "foreign_count" in cols:
        keep.append("foreign_count = 0")
    if "moz_keywords" in tables:
        keep.append("id NOT IN (SELECT place_id FROM moz_keywords WHERE place_id IS NOT NULL)")

    sql = [f"DELETE FROM {t}" for t in ("moz_historyvisits", "moz_inputhistory",
           "moz_places_metadata_search_queries", "moz_places_metadata") if t in tables]
    sql.append("DELETE FROM moz_places WHERE " + " AND ".join(keep))
    sql.append("UPDATE moz_places SET visit_count = 0"
               + (", last_visit_date = NULL" if "last_visit_date" in cols else ""))
    if "moz_annos" in tables:
        sql.append("DELETE FROM moz_annos WHERE place_id NOT IN (SELECT id FROM moz_places)")
    if "moz_origins" in tables and "origin_id" in cols:
        sql.append("DELETE FROM moz_origins WHERE id NOT IN "
                   "(SELECT origin_id FROM moz_places WHERE origin_id IS NOT NULL)")
    return sql


def count_places_history(db: str) -> int:
    con = sqlite3.connect(Path(db).as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        return con.execute("SELECT COUNT(*) FROM moz_historyvisits").fetchone()[0]
    finally:
        con.close()


def clear_places_history(db: str) -> int:
    """Remove all visits from a Firefox places.sqlite, keeping bookmarks. Returns visits removed."""
    con = sqlite3.connect(db, timeout=5, isolation_level=None)
    try:
        visits = con.execute("SELECT COUNT(*) FROM moz_historyvisits").fetchone()[0]
        con.execute("BEGIN")
        try:
            for statement in _places_statements(con):
                con.execute(statement)
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        con.execute("VACUUM")
        return visits
    finally:
        con.close()


def _places_part(profiles) -> Custom:
    def run(fn) -> Stats:
        stats = Stats()
        for prof, _ in profiles():
            db = os.path.join(prof, "places.sqlite")
            if not os.path.isfile(db):
                continue
            try:
                stats.entries += fn(db)
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc):
                    stats.note("Firefox is running - close it to clean history.")
                else:
                    stats.note(f"places.sqlite: {exc}")
        return stats

    return Custom(lambda: run(count_places_history), lambda: run(clear_places_history))


def _firefox_targets():
    profiles = firefox_profiles
    group, procs = "Mozilla Firefox", ("firefox.exe",)

    def files_in_profiles(names):
        return lambda: [p for prof, _ in profiles() for p in existing(prof, names)]

    def cache():
        return [p for _, cache in profiles()
                for d in existing(cache, ["cache2", "startupCache", "thumbnails", "jumpListCache", "OfflineCache"])
                for p in children(d)]

    def sessions():
        return [p for prof, _ in profiles()
                for p in existing(prof, ["sessionstore.jsonlz4"]) + children(os.path.join(prof, "sessionstore-backups"))]

    yield Target("firefox.history", group, "Browsing & form history",
                 "Visited pages and search/form autocomplete in every profile. Bookmarks are kept. "
                 "Firefox must be closed.",
                 [_places_part(profiles), Files(files_in_profiles(["formhistory.sqlite"]))], processes=procs)
    yield Target("firefox.cache", group, "Cache", "Cached web content and page thumbnails.",
                 [Files(cache)], processes=procs)
    yield Target("firefox.cookies", group, "Cookies", "Signs you out of all websites.",
                 [Files(files_in_profiles(["cookies.sqlite", "cookies.sqlite-wal", "cookies.sqlite-shm"]))],
                 default=False, processes=procs)
    yield Target("firefox.sessions", group, "Saved tabs / session", "Tabs restored on next start.",
                 [Files(sessions)], default=False, processes=procs)


def targets():
    for browser in CHROMIUM:
        if browser.profiles():
            yield from _chromium_targets(browser)
    if firefox_profiles():
        yield from _firefox_targets()
