"""winapp2.ini support: community cleaning rules for thousands of applications.

Rules come from https://github.com/MoscaDotTo/Winapp2 (CC BY-SA 4.0). The file is
downloaded on request to %APPDATA%\\PCCleaner\\winapp2.ini; only entries whose program is
detected on this PC are shown, and all of them start unticked.

Supported keys: Detect, DetectFile, DetectOS, SpecialDetect, FileKey (with RECURSE /
REMOVESELF), RegKey, ExcludeKey (FILE / PATH / REG), LangSecRef, Section, Warning.

Extra safety on top of the format:
  * recursive or match-everything rules on protected folders (Windows, your profile,
    AppData, Program Files, ...) are refused - fsutil.is_safe guards every deletion too;
  * RegKeys are only honoured under HKEY_CURRENT_USER.
"""
from __future__ import annotations

import fnmatch
import glob
import itertools
import os
import re
import sys
import time
import urllib.request
import winreg
from dataclasses import dataclass, field
from functools import lru_cache

from .. import fsutil, regutil, winutil
from ..core import Target
from ..stats import Stats

WINAPP2_URL = "https://raw.githubusercontent.com/MoscaDotTo/Winapp2/master/Winapp2.ini"
WINAPP2_FILE = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "PCCleaner", "winapp2.ini")

SECTIONS = {
    "3021": "Applications", "3022": "Internet", "3023": "Multimedia", "3024": "Utilities",
    "3025": "Windows", "3026": "Firefox", "3027": "Opera", "3028": "Safari", "3029": "Google Chrome",
    "3030": "Thunderbird", "3031": "Windows Store", "3032": "CCleaner Browser", "3033": "Microsoft Edge",
    "3034": "Brave", "3035": "Opera GX", "3036": "Spotify", "3037": "Avast Secure Browser",
    "3038": "AVG Secure Browser", "3039": "Arc", "3043": "Norton Secure Browser",
    "3044": "Avira Secure Browser", "3006": "Microsoft Edge",
}

# Files changed within this many hours are left alone, so programs that are running
# right now don't lose files they are using (rules like "%Temp%\*|*|REMOVESELF").
MIN_AGE_HOURS = 1


# ---- parsing ------------------------------------------------------------------

@dataclass
class Entry:
    name: str
    keys: dict[str, list[str]] = field(default_factory=dict)  # "filekey" -> [value, ...]

    def get(self, key: str) -> list[str]:
        return self.keys.get(key, [])


def parse(text: str) -> list[Entry]:
    entries: list[Entry] = []
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip()
            current = Entry(name[:-1].rstrip() if name.endswith("*") else name)
            entries.append(current)
        elif current is not None and "=" in line:
            key, value = line.split("=", 1)
            base = re.sub(r"\d+$", "", key.strip()).lower()  # FileKey12 -> filekey
            current.keys.setdefault(base, []).append(value.strip())
    return entries


# ---- paths ----------------------------------------------------------------------

_FOLDER_IDS = {
    "documents": "FDD39AD0-238F-46AF-ADB4-6C85480369C7", "music": "4BD8D571-6D19-48D3-BE97-422220080E43",
    "pictures": "33E28130-4E1E-4676-835A-98395C3BC3BB", "video": "18989B1D-99B5-455B-841C-AB7C74E4DDFC",
    "desktop": "B4BFCC3A-DB2C-424C-B029-7FE99A87C641",
}


@lru_cache(maxsize=1)
def _variables() -> dict[str, list[str]]:
    env = os.environ
    local, profile = env["LOCALAPPDATA"], env["USERPROFILE"]
    progdata, windir = env.get("ProgramData", r"C:\ProgramData"), env.get("SystemRoot", r"C:\Windows")

    def both(a, b):  # 64-bit and 32-bit locations, without duplicates
        return list(dict.fromkeys(p for p in (env.get(a), env.get(b)) if p))

    v = {
        "appdata": [env["APPDATA"]], "localappdata": [local],
        "locallowappdata": [os.path.join(os.path.dirname(local), "LocalLow")],
        "userprofile": [profile], "commonappdata": [progdata], "allusersprofile": [progdata],
        "programdata": [progdata], "windir": [windir], "systemroot": [windir],
        "systemdrive": [env.get("SystemDrive", "C:")], "public": [env.get("PUBLIC", r"C:\Users\Public")],
        "temp": [env.get("TEMP", "")], "tmp": [env.get("TMP", "")],
        "programfiles": both("ProgramW6432", "ProgramFiles(x86)") or [env.get("ProgramFiles", "")],
        "commonprogramfiles": both("CommonProgramW6432", "CommonProgramFiles(x86)"),
    }
    for name, guid in _FOLDER_IDS.items():
        v[name] = [winutil.known_folder(guid) or os.path.join(profile, name.capitalize())]
    return {k: [p for p in paths if p] for k, paths in v.items()}


def expand(path: str) -> list[str]:
    """Expand %Variables% (some have two alternatives, e.g. Program Files x64/x86)."""
    names = re.findall(r"%([^%]+)%", path)
    options = []
    for name in names:
        values = _variables().get(name.lower()) or ([os.environ[name]] if name in os.environ else [])
        if not values:
            return []
        options.append(values)
    results = []
    for combo in itertools.product(*options):
        result = path
        for name, value in zip(names, combo):
            result = result.replace(f"%{name}%", value, 1)
        results.append(result.rstrip("\\") or result)
    return results


def _has_wildcard(s: str) -> bool:
    return "*" in s or "?" in s


def resolve(path: str) -> list[str]:
    """Expand variables and wildcards in folder names into existing paths."""
    found = []
    for p in expand(path):
        if not _has_wildcard(p):
            if os.path.lexists(p):
                found.append(p)
            continue
        pattern = "\\".join(s if _has_wildcard(s) else glob.escape(s) for s in p.split("\\"))
        found.extend(glob.glob(pattern, include_hidden=True))
    return found


# ---- detection ---------------------------------------------------------------------

_HIVES = {"HKCU": winreg.HKEY_CURRENT_USER, "HKEY_CURRENT_USER": winreg.HKEY_CURRENT_USER,
          "HKLM": winreg.HKEY_LOCAL_MACHINE, "HKEY_LOCAL_MACHINE": winreg.HKEY_LOCAL_MACHINE,
          "HKCR": winreg.HKEY_CLASSES_ROOT, "HKEY_CLASSES_ROOT": winreg.HKEY_CLASSES_ROOT,
          "HKU": winreg.HKEY_USERS, "HKEY_USERS": winreg.HKEY_USERS}

_SPECIAL = {
    "DET_CHROME": lambda: os.path.isdir(os.path.join(os.environ["LOCALAPPDATA"], r"Google\Chrome\User Data")),
    "DET_MOZILLA": lambda: os.path.isdir(os.path.join(os.environ["APPDATA"], r"Mozilla\Firefox")),
    "DET_THUNDERBIRD": lambda: os.path.isdir(os.path.join(os.environ["APPDATA"], "Thunderbird")),
    "DET_OPERA": lambda: os.path.isdir(os.path.join(os.environ["APPDATA"], "Opera Software")),
    "DET_WINSTORE": lambda: True,
}


def _split_hive(path: str):
    hive_name, _, sub = path.partition("\\")
    return _HIVES.get(hive_name.upper()), sub


def _reg_key_exists(path: str) -> bool:
    hive, sub = _split_hive(path)
    if hive is None:
        return False
    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            winreg.CloseKey(winreg.OpenKey(hive, sub, 0, winreg.KEY_READ | view))
            return True
        except OSError:
            pass
    return False


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", text)[:2]) or (0,)


def _os_matches(value: str) -> bool:
    current = sys.getwindowsversion()[:2]
    low, _, high = value.partition("|")
    return (not low.strip() or current >= _version(low)) and (not high.strip() or current <= _version(high))


def detected(entry: Entry) -> bool:
    if not all(_os_matches(v) for v in entry.get("detectos")):
        return False
    checks = ([lambda v=v: _reg_key_exists(v) for v in entry.get("detect")]
              + [lambda v=v: bool(resolve(v)) for v in entry.get("detectfile")]
              + [lambda v=v: _SPECIAL.get(v.upper(), lambda: False)() for v in entry.get("specialdetect")])
    return not checks or any(check() for check in checks)


# ---- cleaning ------------------------------------------------------------------------

@dataclass
class FileRule:
    path: str
    patterns: list[str]
    recurse: bool
    removeself: bool


def parse_filekey(value: str) -> FileRule:
    parts = value.split("|")
    patterns = [p.strip() for p in (parts[1] if len(parts) > 1 else "*").split(";") if p.strip()]
    patterns = ["*" if p == "*.*" else p for p in patterns] or ["*"]  # Windows "*.*" means everything
    flags = {f.strip().upper() for f in parts[2:]}
    removeself = "REMOVESELF" in flags
    return FileRule(parts[0].strip(), patterns, removeself or "RECURSE" in flags, removeself)


@dataclass
class Exclusion:
    kind: str           # FILE / PATH / REG
    paths: list[str]    # normalised (fnmatch patterns for FILE/PATH)
    names: list[str]


def parse_excludekey(value: str) -> Exclusion | None:
    parts = value.split("|")
    if len(parts) < 2:
        return None
    kind = parts[0].strip().upper()
    if kind == "REG":
        return Exclusion(kind, [parts[1].strip().rstrip("\\").lower()], [])
    if kind not in ("FILE", "PATH"):
        return None
    paths = [os.path.normcase(p.replace("[", "[[]")) for p in expand(parts[1].strip())]
    names = [n.strip() for n in (parts[2] if len(parts) > 2 else "").split(";") if n.strip()]
    return Exclusion(kind, paths, ["*" if n == "*.*" else n for n in names])


def is_excluded(file_path: str, exclusions: list[Exclusion]) -> bool:
    folder, name = os.path.split(os.path.normcase(file_path))
    for ex in exclusions:
        if ex.kind == "FILE":
            in_place = any(fnmatch.fnmatch(folder, p) for p in ex.paths)
        elif ex.kind == "PATH":
            in_place = any(fnmatch.fnmatch(folder, p) or fnmatch.fnmatch(folder, p + "\\*") for p in ex.paths)
        else:
            continue
        if in_place and (not ex.names or any(fnmatch.fnmatch(name, n) for n in ex.names)):
            return True
    return False


def _rule_allowed(base: str, rule: FileRule) -> bool:
    """Protected folders may only be cleaned of specific file types, never recursively."""
    return fsutil.is_safe(base) or (not rule.recurse and "*" not in rule.patterns)


def _matching_files(base: str, rule: FileRule):
    stack = [base]
    while stack:
        folder = stack.pop()
        try:
            with os.scandir(folder) as it:
                entries = list(it)
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_symlink() or e.is_junction():
                    continue  # never follow or delete links
                if e.is_dir(follow_symlinks=False):
                    if rule.recurse:
                        stack.append(e.path)
                elif any(fnmatch.fnmatch(e.name, p) for p in rule.patterns):
                    yield e.path
            except OSError:
                continue


def _remove_empty_dirs(base: str) -> None:
    for folder, _dirs, _files in os.walk(base, topdown=False):
        if fsutil.is_safe(folder):
            try:
                os.rmdir(folder)
            except OSError:
                pass


_PROTECTED_REG = {k.lower() for k in (
    r"Software\Microsoft", r"Software\Microsoft\Windows", r"Software\Microsoft\Windows\CurrentVersion",
    r"Software\Microsoft\Windows\CurrentVersion\Explorer", r"Software\Microsoft\Windows NT",
    r"Software\Microsoft\Windows NT\CurrentVersion", r"Software\Microsoft\Office", r"Software\Classes",
    r"Software\Policies", r"Software\Wow6432Node", r"Control Panel", r"Environment",
    r"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts",  # "Open with" / default apps
)}


class Winapp2Part:
    def __init__(self, entry: Entry, min_age_hours: float = MIN_AGE_HOURS):
        self.min_age_hours = min_age_hours
        self.file_rules = [parse_filekey(v) for v in entry.get("filekey")]
        self.reg_rules = [v.partition("|")[::2] for v in entry.get("regkey")]  # (key, value name)
        self.exclusions = [x for x in map(parse_excludekey, entry.get("excludekey")) if x]

    def _files(self, stats: Stats):
        """Yield (base folder, rule, file) for every file this entry would delete."""
        for rule in self.file_rules:
            for base in resolve(rule.path):
                if not os.path.isdir(base):
                    continue
                if not _rule_allowed(base, rule):
                    stats.note(f"Skipped unsafe rule on protected folder: {base}")
                    continue
                for path in _matching_files(base, rule):
                    if not is_excluded(path, self.exclusions):
                        yield base, rule, path

    def _registry(self, stats: Stats):
        """Yield (key path under HKCU, value name or '') for every existing registry target."""
        reg_excludes = [p for x in self.exclusions if x.kind == "REG" for p in x.paths]
        for key, value in self.reg_rules:
            hive, sub = _split_hive(key.strip().rstrip("\\"))
            if hive != winreg.HKEY_CURRENT_USER or _has_wildcard(sub):
                stats.note(f"Skipped registry rule outside HKCU: {key}")
                continue
            if sub.lower() in _PROTECTED_REG or sub.count("\\") < 1:
                stats.note(f"Skipped registry rule on a core key: {key}")
                continue
            full = key.strip().rstrip("\\").lower()
            if any(full == x or full.startswith(x + "\\") for x in reg_excludes):
                continue
            if value.strip():
                if regutil.get_value(sub, value.strip()) is not None:
                    yield sub, value.strip()
            elif regutil.key_exists(sub):
                yield sub, ""

    def _cutoff(self) -> float | None:
        return time.time() - self.min_age_hours * 3600 if self.min_age_hours else None

    def scan(self) -> Stats:
        stats, cutoff = Stats(), self._cutoff()
        for _base, _rule, path in self._files(stats):
            fsutil.measure(path, stats, cutoff)
        stats.entries += sum(1 for _ in self._registry(stats))
        return stats

    def clean(self) -> Stats:
        stats, cutoff = Stats(), self._cutoff()
        removeself_bases = set()
        for base, rule, path in list(self._files(stats)):
            fsutil.remove(path, stats, cutoff)
            if rule.removeself:
                removeself_bases.add(base)
        for base in removeself_bases:
            _remove_empty_dirs(base)
        for sub, value in list(self._registry(stats)):
            if value:
                regutil.delete_value(sub, value)
            else:
                regutil.delete_key(sub)
            stats.entries += 1
        return stats


def _make_target(entry: Entry, target_id: str, min_age_hours: float) -> Target:
    ref = (entry.get("langsecref") or [""])[0]
    group = (entry.get("section") or [SECTIONS.get(ref, "Other")])[0]
    warning = " ".join(entry.get("warning"))
    paths = [parse_filekey(v).path for v in entry.get("filekey")] + [v.partition("|")[0] for v in entry.get("regkey")]
    shown = ", ".join(paths[:4]) + (f" and {len(paths) - 4} more" if len(paths) > 4 else "")
    desc = f"Community rule from winapp2.ini. Cleans: {shown}." + (f"\nWarning: {warning}" if warning else "")
    return Target(target_id, f"winapp2: {group}", entry.name, desc, [Winapp2Part(entry, min_age_hours)],
                  default=False, warning=warning)


# ---- loading / updating ------------------------------------------------------------

def load(path: str = WINAPP2_FILE) -> list[Entry]:
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        return parse(f.read())


def targets(path: str = WINAPP2_FILE, min_age_hours: float = MIN_AGE_HOURS):
    used: set[str] = set()
    for entry in load(path):
        if not (entry.get("filekey") or entry.get("regkey")):
            continue
        try:
            if not detected(entry):
                continue
        except Exception:
            continue
        base = "w2." + (re.sub(r"[^a-z0-9]+", "_", entry.name.lower()).strip("_") or "entry")
        target_id = base
        for n in itertools.count(2):
            if target_id not in used:
                break
            target_id = f"{base}_{n}"
        used.add(target_id)
        yield _make_target(entry, target_id, min_age_hours)


def download(url: str = WINAPP2_URL, dest: str = WINAPP2_FILE, timeout: int = 60) -> int:
    """Fetch the latest winapp2.ini. Returns the number of entries in it."""
    request = urllib.request.Request(url, headers={"User-Agent": "PCCleaner"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read()
    entries = parse(data.decode("utf-8-sig", errors="replace"))
    if len(entries) < 100 or not any(e.get("filekey") for e in entries):
        raise ValueError("The downloaded file does not look like winapp2.ini")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".download"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, dest)
    return len(entries)
