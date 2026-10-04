"""Windows API helpers: elevation, running processes, recycle bin, clipboard."""
from __future__ import annotations

import csv
import ctypes
import os
import subprocess
import sys
from ctypes import wintypes

_shell32 = ctypes.windll.shell32
_user32 = ctypes.windll.user32


def is_admin() -> bool:
    try:
        return bool(_shell32.IsUserAnAdmin())
    except OSError:
        return False


def relaunch_as_admin() -> bool:
    """Start a new elevated copy of the app (UAC prompt). True if it launched."""
    if getattr(sys, "frozen", False):  # PyInstaller exe
        exe, params, cwd = sys.executable, subprocess.list2cmdline(sys.argv[1:]), None
    else:
        exe = sys.executable
        pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(pythonw):
            exe = pythonw
        params = "-m pccleaner"
        cwd = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return _shell32.ShellExecuteW(None, "runas", exe, params, cwd, 1) > 32


def running_processes() -> set[str]:
    """Lower-case executable names of all running processes."""
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
            errors="replace", creationflags=subprocess.CREATE_NO_WINDOW, timeout=15,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    return {row[0].lower() for row in csv.reader(out.splitlines()) if row}


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


def known_folder(guid: str) -> str | None:
    """Path of a Windows known folder (follows OneDrive / user redirection)."""
    import uuid
    gid = _GUID.from_buffer_copy(uuid.UUID(guid).bytes_le)
    path = ctypes.c_wchar_p()
    if _shell32.SHGetKnownFolderPath(ctypes.byref(gid), 0, None, ctypes.byref(path)) != 0:
        return None
    try:
        return path.value
    finally:
        ctypes.windll.ole32.CoTaskMemFree(path)


def run_powershell(script: str, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW, timeout=timeout,
    )


# ---- Recycle Bin ------------------------------------------------------------

class _SHQUERYRBINFO(ctypes.Structure):
    if ctypes.sizeof(ctypes.c_void_p) == 4:
        _pack_ = 1  # shellapi.h packs this struct on 32-bit only
    _fields_ = [("cbSize", wintypes.DWORD),
                ("i64Size", ctypes.c_longlong),
                ("i64NumItems", ctypes.c_longlong)]


def recycle_bin_info() -> tuple[int, int]:
    """(bytes, item count) across all drives."""
    info = _SHQUERYRBINFO(cbSize=ctypes.sizeof(_SHQUERYRBINFO))
    if _shell32.SHQueryRecycleBinW(None, ctypes.byref(info)) != 0:
        return 0, 0
    return info.i64Size, info.i64NumItems


def empty_recycle_bin() -> None:
    SHERB_NOCONFIRMATION, SHERB_NOPROGRESSUI, SHERB_NOSOUND = 0x1, 0x2, 0x4
    _shell32.SHEmptyRecycleBinW(None, None, SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND)


# ---- Shell file operations ----------------------------------------------------

class _SHFILEOPSTRUCTW(ctypes.Structure):
    if ctypes.sizeof(ctypes.c_void_p) == 4:
        _pack_ = 1  # shellapi.h packs this struct on 32-bit only
    _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
                ("pFrom", ctypes.c_void_p), ("pTo", ctypes.c_void_p),
                ("fFlags", wintypes.WORD), ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]


FO_DELETE = 0x3
FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI, FOF_WANTNUKEWARNING = 0x10, 0x40, 0x400, 0x4000


def _shell_delete(paths: list[str], flags: int, hwnd=None) -> int:
    buffer = ctypes.create_unicode_buffer("\0".join(os.path.abspath(p) for p in paths) + "\0")  # double-NUL
    op = _SHFILEOPSTRUCTW(hwnd=hwnd, wFunc=FO_DELETE, pFrom=ctypes.addressof(buffer), fFlags=flags)
    return _shell32.SHFileOperationW(ctypes.byref(op))


def send_to_recycle_bin(paths: list[str], hwnd=None) -> list[str]:
    """Move files to the Recycle Bin. Windows asks before permanently deleting anything that is
    too big for the bin. Returns the paths that still exist afterwards (not recycled)."""
    if paths:
        _shell_delete(paths, FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_WANTNUKEWARNING | FOF_NOERRORUI, hwnd)
    return [p for p in paths if os.path.lexists(p)]


def show_in_explorer(path: str) -> None:
    subprocess.Popen(f'explorer /select,"{path}"')


# ---- Clipboard --------------------------------------------------------------

def clipboard_has_data() -> bool:
    return _user32.CountClipboardFormats() > 0


_CLIPBOARD_WINRT = ("[Windows.ApplicationModel.DataTransfer.Clipboard,"
                    "Windows.ApplicationModel.DataTransfer,ContentType=WindowsRuntime]")


def clear_clipboard_history() -> bool:
    """Clear the Win+V clipboard history (pinned items are kept by Windows)."""
    result = run_powershell(f"{_CLIPBOARD_WINRT}::ClearHistory()", timeout=60)
    return result.stdout.strip().lower() == "true"


def clear_clipboard() -> bool:
    if not _user32.OpenClipboard(None):
        return False
    try:
        return bool(_user32.EmptyClipboard())
    finally:
        _user32.CloseClipboard()
