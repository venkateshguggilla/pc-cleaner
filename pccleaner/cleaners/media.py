"""Media players: recently played lists."""
from __future__ import annotations

import csv
import os

from .. import regutil
from ..core import Custom, Files, RegKeys, Target
from ..fsutil import children, existing
from ..stats import Stats

GROUP = "Media players"
ROAMING = os.environ.get("APPDATA", "")

# ---- VLC: recent lists live in a Qt ini file ---------------------------------

VLC_INI = os.path.join(ROAMING, "vlc", "vlc-qt-interface.ini")
# VLC 3 uses [RecentsMRL]; "times" holds the resume positions of those files.
_VLC_KEYS = {"recentsmrl": {"list", "times"}, "recentsmru": {"list", "times"}, "opendialog": {"netmru"}}
_VLC_COUNTED = {"list", "netmru"}


def _vlc_matches(lines):
    """Yield (line index, key, value) for every history line in the ini."""
    section = None
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            section = s[1:-1].lower()
            continue
        key, sep, value = s.partition("=")
        key = key.strip().lower()
        if sep and key in _VLC_KEYS.get(section, ()):
            yield i, key, value.strip()


def _vlc_count(value: str) -> int:
    if not value or value.startswith("@Invalid"):
        return 0
    return sum(1 for item in next(csv.reader([value], skipinitialspace=True)) if item)


def _read_lines(path):
    with open(path, encoding="utf-8", errors="surrogateescape", newline="") as f:
        return f.read().splitlines(keepends=True)


def vlc_scan(path: str = VLC_INI) -> Stats:
    if not os.path.isfile(path):
        return Stats()
    lines = _read_lines(path)
    return Stats(entries=sum(_vlc_count(v) for _, k, v in _vlc_matches(lines) if k in _VLC_COUNTED))


def vlc_clean(path: str = VLC_INI) -> Stats:
    if not os.path.isfile(path):
        return Stats()
    lines = _read_lines(path)
    matches = list(_vlc_matches(lines))
    if matches:
        drop = {i for i, _, _ in matches}
        with open(path, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
            f.writelines(line for i, line in enumerate(lines) if i not in drop)
    return Stats(entries=sum(_vlc_count(v) for _, k, v in matches if k in _VLC_COUNTED))


# ---- player table --------------------------------------------------------------

def targets():
    wmp = r"Software\Microsoft\MediaPlayer\Player"
    if regutil.key_exists(wmp + r"\RecentFileList") or regutil.key_exists(wmp + r"\RecentURLList"):
        yield Target("media.wmp", GROUP, "Windows Media Player: recent files",
                     "Recently opened files and URLs.",
                     [RegKeys(wmp + r"\RecentFileList", wmp + r"\RecentURLList")],
                     processes=("wmplayer.exe",))

    if os.path.isfile(VLC_INI):
        yield Target("media.vlc", GROUP, "VLC: recently played",
                     "'Media > Open Recent Media' list, remembered resume positions, "
                     "network stream history and crash dumps.",
                     [Custom(vlc_scan, vlc_clean),
                      Files(lambda: children(os.path.join(ROAMING, "vlc", "crashdump")))],
                     processes=("vlc.exe",))

    mpc_hc = r"Software\MPC-HC\MPC-HC"
    if regutil.key_exists(mpc_hc):
        yield Target("media.mpc_hc", GROUP, "MPC-HC: recent files",
                     "Recent file list and remembered playback positions.",
                     [RegKeys(mpc_hc + r"\Recent File List"),
                      RegKeys(mpc_hc + r"\MediaHistory", count="subkeys")],
                     processes=("mpc-hc.exe", "mpc-hc64.exe"))

    mpc_be_dir = os.path.join(ROAMING, "MPC-BE")
    if regutil.key_exists(r"Software\MPC-BE") or os.path.isdir(mpc_be_dir):
        yield Target("media.mpc_be", GROUP, "MPC-BE: recent files",
                     "Recent file list and playback history.",
                     [RegKeys(r"Software\MPC-BE\Recent File List"),
                      Files(lambda: existing(mpc_be_dir, ["history.mpc_lst"]))],
                     processes=("mpc-be.exe", "mpc-be64.exe"))

    pot_keys = [rf"Software\DAUM\{v}\RememberFiles" for v in ("PotPlayerMini64", "PotPlayerMini")]
    pot_dirs = [os.path.join(ROAMING, v, "Playlist") for v in ("PotPlayerMini64", "PotPlayerMini")]
    if any(map(regutil.key_exists, pot_keys)) or any(map(os.path.isdir, pot_dirs)):
        yield Target("media.potplayer", GROUP, "PotPlayer: history & playlists",
                     "Remembered files/positions and saved default playlists.",
                     [RegKeys(*pot_keys, count="subkeys"),
                      Files(lambda: [p for d in pot_dirs for p in children(d)])],
                     processes=("potplayermini64.exe", "potplayermini.exe"))
