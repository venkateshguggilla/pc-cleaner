"""Windows Explorer / shell usage history."""
from __future__ import annotations

import os

from .. import regutil, winutil
from ..core import Custom, Files, RegKeys, Target
from ..fsutil import children, glob_in
from ..stats import Stats

GROUP = "Windows Explorer"
EXPLORER = r"Software\Microsoft\Windows\CurrentVersion\Explorer"


_LOCAL_SHELL = r"Software\Classes\Local Settings\Software\Microsoft\Windows\Shell"
_USERASSIST = EXPLORER + r"\UserAssist"


def _clipboard_clean() -> Stats:
    had = winutil.clipboard_has_data()
    winutil.clear_clipboard()
    return Stats(entries=int(had))


def _clip_history_enabled() -> bool:
    return regutil.get_value(r"Software\Microsoft\Clipboard", "EnableClipboardHistory", 0) == 1


def _clip_history_scan() -> Stats:
    stats = Stats()
    if _clip_history_enabled():
        stats.note("Clipboard history is on - it will be cleared")
    else:
        stats.note("Clipboard history is turned off")
    return stats


def _clip_history_clean() -> Stats:
    stats = Stats()
    if _clip_history_enabled():
        stats.note("Clipboard history cleared" if winutil.clear_clipboard_history()
                   else "Could not clear clipboard history")
    return stats


def _userassist_keys() -> list[str]:
    return [f"{_USERASSIST}\\{guid}\\Count" for guid in regutil.subkeys(_USERASSIST)]


def _userassist_scan() -> Stats:
    return Stats(entries=sum(len(regutil.value_names(k)) for k in _userassist_keys()))


def _userassist_clean() -> Stats:
    stats = _userassist_scan()
    for key in _userassist_keys():
        regutil.clear_values(key)
    return stats


def targets():
    recent = os.path.join(os.environ["APPDATA"], r"Microsoft\Windows\Recent")

    yield Target(
        "explorer.recent_docs", GROUP, "Recent documents",
        "The 'Recent files' list in Explorer / Quick access and the Start menu.",
        [Files(lambda: glob_in(recent, "*.lnk")), RegKeys(EXPLORER + r"\RecentDocs")])

    yield Target(
        "explorer.jump_lists", GROUP, "Taskbar jump lists",
        "Recent items shown when right-clicking taskbar icons, including frequent "
        "folders in Quick access. Note: this also removes items pinned in jump lists.",
        [Files(lambda: children(os.path.join(recent, "AutomaticDestinations"))
               + children(os.path.join(recent, "CustomDestinations")))],
        default=False)

    yield Target(
        "explorer.run_mru", GROUP, "Run dialog history",
        "Commands typed into Win+R.", [RegKeys(EXPLORER + r"\RunMRU")])

    yield Target(
        "explorer.typed_paths", GROUP, "Address bar history",
        "Paths typed into the Explorer address bar.", [RegKeys(EXPLORER + r"\TypedPaths")])

    yield Target(
        "explorer.search", GROUP, "Explorer search history",
        "Terms typed into the Explorer search box.", [RegKeys(EXPLORER + r"\WordWheelQuery")])

    yield Target(
        "explorer.dialogs", GROUP, "Open / Save dialog history",
        "Recently used files and folders remembered by Open and Save As dialogs.",
        [RegKeys(EXPLORER + r"\ComDlg32\OpenSavePidlMRU", EXPLORER + r"\ComDlg32\LastVisitedPidlMRU")])

    yield Target(
        "explorer.clipboard", GROUP, "Clipboard",
        "Whatever is currently copied to the clipboard.",
        [Custom(lambda: Stats(entries=int(winutil.clipboard_has_data())), _clipboard_clean)])

    yield Target(
        "explorer.clipboard_history", GROUP, "Clipboard history (Win+V)",
        "Items saved in the Win+V clipboard history. Pinned items are kept.",
        [Custom(_clip_history_scan, _clip_history_clean)])

    yield Target(
        "explorer.shellbags", GROUP, "Folder history (ShellBags)",
        "Windows' record of every folder you have opened, kept even after the folder or drive is gone. "
        "Folders go back to their default view (icons, columns, sort order). "
        "Takes full effect after signing out or restarting Explorer.",
        [RegKeys(r"Software\Microsoft\Windows\Shell\BagMRU", _LOCAL_SHELL + r"\BagMRU", count="tree"),
         RegKeys(r"Software\Microsoft\Windows\Shell\Bags", _LOCAL_SHELL + r"\Bags", count="none")],
        default=False)

    yield Target(
        "explorer.userassist", GROUP, "Program usage history (UserAssist)",
        "Windows' record of which programs you ran, how often and when. "
        "This also resets the Start menu's 'Most used' list.",
        [Custom(_userassist_scan, _userassist_clean)], default=False)

    rdp = r"Software\Microsoft\Terminal Server Client"
    if regutil.key_exists(rdp + r"\Default") or regutil.key_exists(rdp + r"\Servers"):
        yield Target(
            "explorer.rdp", GROUP, "Remote Desktop history",
            "Computers listed in the Remote Desktop Connection drop-down.",
            [RegKeys(rdp + r"\Default"), RegKeys(rdp + r"\Servers", count="none")])

    if regutil.key_exists(EXPLORER + r"\Map Network Drive MRU"):
        yield Target(
            "explorer.network_drives", GROUP, "Map Network Drive history",
            "Network paths typed into the Map Network Drive dialog.",
            [RegKeys(EXPLORER + r"\Map Network Drive MRU")])

    applets = r"Software\Microsoft\Windows\CurrentVersion\Applets"
    applet_keys = [applets + rf"\{app}\Recent File List" for app in ("Paint", "Wordpad")]
    if any(map(regutil.key_exists, applet_keys)):
        yield Target(
            "explorer.paint_wordpad", GROUP, "Paint & WordPad recent files",
            "Recent files list in classic Paint and WordPad.",
            [RegKeys(*applet_keys)], processes=("mspaint.exe", "wordpad.exe"))
