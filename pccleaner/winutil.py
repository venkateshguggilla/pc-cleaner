"""Windows API helpers: elevation, running processes, recycle bin, clipboard."""
from __future__ import annotations

import csv
import ctypes
import os
import subprocess
import sys
import time
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


# ---- shortcuts (.lnk) via the shell's IShellLink COM object, no scripts needed ----------

def _com_method(ptr, index: int, *argtypes):
    """Callable for method number `index` of a COM interface pointer's vtable."""
    vtable = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    proto = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)
    return lambda *args: proto(vtable[index])(ptr, *args)


def _guid(text: str) -> "_GUID":
    import uuid
    return _GUID.from_buffer_copy(uuid.UUID(text).bytes_le)


def create_shortcut(lnk_path: str, target: str, arguments: str = "", workdir: str = "",
                    icon: str = "", description: str = "") -> None:
    ole32 = ctypes.windll.ole32
    ole32.CoInitialize(None)  # fine if COM is already initialised on this thread
    link = ctypes.c_void_p()
    hr = ole32.CoCreateInstance(ctypes.byref(_guid("00021401-0000-0000-C000-000000000046")), None,
                                1, ctypes.byref(_guid("000214F9-0000-0000-C000-000000000046")),  # IShellLinkW
                                ctypes.byref(link))
    if hr != 0:
        raise OSError(f"Could not create shell link object (HRESULT {hr & 0xFFFFFFFF:#010x})")
    try:
        _com_method(link, 20, wintypes.LPCWSTR)(target)        # SetPath
        _com_method(link, 11, wintypes.LPCWSTR)(arguments)     # SetArguments
        _com_method(link, 9, wintypes.LPCWSTR)(workdir)        # SetWorkingDirectory
        _com_method(link, 7, wintypes.LPCWSTR)(description)    # SetDescription
        if icon:
            _com_method(link, 17, wintypes.LPCWSTR, ctypes.c_int)(icon, 0)  # SetIconLocation
        persist = ctypes.c_void_p()
        _com_method(link, 0, ctypes.c_void_p, ctypes.c_void_p)(  # QueryInterface(IPersistFile)
            ctypes.byref(_guid("0000010B-0000-0000-C000-000000000046")), ctypes.byref(persist))
        try:
            os.makedirs(os.path.dirname(lnk_path), exist_ok=True)
            _com_method(persist, 6, wintypes.LPCWSTR, wintypes.BOOL)(lnk_path, True)  # Save
        finally:
            _com_method(persist, 2)()  # Release
    finally:
        _com_method(link, 2)()  # Release


def find_pids(names) -> dict[int, str]:
    """{pid: exe name} for running processes whose exe name is in `names` (case-insensitive)."""
    wanted = {n.lower() for n in names}
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
            errors="replace", creationflags=subprocess.CREATE_NO_WINDOW, timeout=15,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    pids = {}
    for row in csv.reader(out.splitlines()):
        if len(row) > 1 and row[0].lower() in wanted and row[1].isdigit() and int(row[1]) != os.getpid():
            pids[int(row[1])] = row[0].lower()
    return pids


_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def close_windows(pids) -> int:
    """Politely ask processes to quit: send WM_CLOSE to their visible top-level windows,
    as if the user clicked X (apps can save state or ask about unsaved work)."""
    pids = set(pids)
    sent = 0

    def callback(hwnd, _):
        nonlocal sent
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and _user32.IsWindowVisible(hwnd):
            _user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
            sent += 1
        return True

    _user32.EnumWindows(_WNDENUMPROC(callback), 0)
    return sent


def wait_for_exit(pids, timeout: float) -> set[int]:
    """Wait until the processes end. Returns the pids still running after `timeout` seconds."""
    kernel32 = ctypes.windll.kernel32
    SYNCHRONIZE, WAIT_TIMEOUT = 0x00100000, 0x102
    deadline = time.monotonic() + timeout
    remaining = set()
    for pid in pids:
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            continue  # already gone (or not ours to watch)
        try:
            wait_ms = max(0, int((deadline - time.monotonic()) * 1000))
            if kernel32.WaitForSingleObject(handle, wait_ms) == WAIT_TIMEOUT:
                remaining.add(pid)
        finally:
            kernel32.CloseHandle(handle)
    return remaining


def kill_pids(pids) -> None:
    """Force-end processes (and their child processes). Unsaved work in them is lost."""
    for pid in pids:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)


def close_processes(names, timeout: float = 15.0) -> set[str]:
    """Close all running programs with these exe names gracefully.
    Returns the exe names that are still running afterwards."""
    pids = find_pids(names)
    if pids:
        close_windows(pids)
        wait_for_exit(pids, timeout)
    return set(find_pids(names).values())


def kill_processes(names) -> set[str]:
    """Force-close all running programs with these exe names. Returns names still running."""
    pids = find_pids(names)
    if pids:
        kill_pids(pids)
        wait_for_exit(pids, 10)
    return set(find_pids(names).values())


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


def set_dark_title_bar(hwnd: int, dark: bool) -> None:
    """Ask Windows 10 (20H1+) / 11 to draw a window's title bar dark or light."""
    value = ctypes.c_int(1 if dark else 0)
    for attribute in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (19 on older Windows 10 builds)
        if ctypes.windll.dwmapi.DwmSetWindowAttribute(wintypes.HWND(hwnd), attribute,
                                                      ctypes.byref(value), ctypes.sizeof(value)) == 0:
            break


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
