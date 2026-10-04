"""Registry helpers for clearing most-recently-used (MRU) lists under HKCU."""
from __future__ import annotations

import winreg

HKCU = winreg.HKEY_CURRENT_USER
_INDEX_VALUES = {"", "mrulist", "mrulistex"}  # bookkeeping values, not real entries


def _value_names(key) -> list[str]:
    names, i = [], 0
    while True:
        try:
            names.append(winreg.EnumValue(key, i)[0])
        except OSError:
            return names
        i += 1


def _subkey_names(key) -> list[str]:
    names, i = [], 0
    while True:
        try:
            names.append(winreg.EnumKey(key, i))
        except OSError:
            return names
        i += 1


def key_exists(path: str, hive=HKCU) -> bool:
    try:
        winreg.CloseKey(winreg.OpenKey(hive, path))
        return True
    except OSError:
        return False


def count_entries(path: str, mode: str = "max", hive=HKCU) -> int:
    """Approximate number of history entries stored in a key.

    mode="max":     MRU style - the key and its subkeys repeat the same list
                    (e.g. RecentDocs keeps "all" plus one list per extension),
                    so report the longest list.
    mode="subkeys": one subkey per entry.
    mode="tree":    every subkey at any depth is an entry (ShellBags: one per folder).
    mode="none":    not counted (data that duplicates another key).
    """
    if mode == "none":
        return 0
    try:
        key = winreg.OpenKey(hive, path)
    except OSError:
        return 0
    with key:
        subkeys = _subkey_names(key)
        if mode == "subkeys":
            return len(subkeys)
        if mode == "tree":
            return sum(1 + count_entries(f"{path}\\{sub}", mode, hive) for sub in subkeys)
        n = sum(1 for v in _value_names(key) if v.lower() not in _INDEX_VALUES)
        for sub in subkeys:
            n = max(n, count_entries(f"{path}\\{sub}", mode, hive))
        return n


def subkeys(path: str, hive=HKCU) -> list[str]:
    try:
        with winreg.OpenKey(hive, path) as key:
            return _subkey_names(key)
    except OSError:
        return []


def value_names(path: str, hive=HKCU) -> list[str]:
    try:
        with winreg.OpenKey(hive, path) as key:
            return _value_names(key)
    except OSError:
        return []


def get_value(path: str, name: str, default=None, hive=HKCU):
    try:
        with winreg.OpenKey(hive, path) as key:
            return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return default


def find_keys(path: str, names: set[str], max_depth: int = 4, hive=HKCU) -> list[str]:
    """Full paths of subkeys of `path` (at any depth up to max_depth) whose name is in `names`."""
    found = []
    if max_depth <= 0:
        return found
    for sub in subkeys(path, hive):
        full = f"{path}\\{sub}"
        if sub.lower() in names:
            found.append(full)
        else:
            found.extend(find_keys(full, names, max_depth - 1, hive))
    return found


def clear_values(path: str, hive=HKCU) -> None:
    """Delete every value of `path`, keeping its subkeys."""
    try:
        key = winreg.OpenKey(hive, path, 0, winreg.KEY_ALL_ACCESS)
    except FileNotFoundError:
        return
    with key:
        for name in _value_names(key):
            winreg.DeleteValue(key, name)


def delete_value(path: str, name: str, hive=HKCU) -> None:
    try:
        with winreg.OpenKey(hive, path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)
    except FileNotFoundError:
        pass


def delete_key(path: str, hive=HKCU) -> None:
    """Delete a key with all its subkeys."""
    parent, _, name = path.rstrip("\\").rpartition("\\")
    if not parent or not name:
        raise ValueError(f"Refusing to delete top-level key: {path}")
    try:
        with winreg.OpenKey(hive, parent, 0, winreg.KEY_ALL_ACCESS) as key:
            _delete_tree(key, name)
    except FileNotFoundError:
        pass


def clear(path: str, hive=HKCU) -> None:
    """Delete every value and subkey of `path`, keeping the (now empty) key."""
    try:
        key = winreg.OpenKey(hive, path, 0, winreg.KEY_ALL_ACCESS)
    except FileNotFoundError:
        return
    with key:
        for name in _value_names(key):
            winreg.DeleteValue(key, name)
        for sub in _subkey_names(key):
            _delete_tree(key, sub)


def _delete_tree(parent, name: str) -> None:
    with winreg.OpenKey(parent, name, 0, winreg.KEY_ALL_ACCESS) as key:
        for sub in _subkey_names(key):
            _delete_tree(key, sub)
    winreg.DeleteKey(parent, name)
