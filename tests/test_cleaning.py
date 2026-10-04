"""Tests run only against throwaway folders, databases and registry keys."""
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
import uuid
import winreg

from pccleaner import fsutil, regutil
from pccleaner.cleaners import apps, browsers, build_catalog, media
from pccleaner.stats import Stats


class FsTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="pccleaner_test_")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def write(self, rel, data=b"x" * 10, age_hours=0):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        if age_hours:
            t = time.time() - age_hours * 3600
            os.utime(path, (t, t))
        return path

    def test_removes_tree_and_counts(self):
        self.write("junk/a.tmp")
        self.write("junk/sub/b.tmp", b"y" * 5)
        stats = Stats()
        fsutil.measure(os.path.join(self.root, "junk"), stats)
        self.assertEqual((stats.files, stats.bytes), (2, 15))
        stats = Stats()
        fsutil.remove(os.path.join(self.root, "junk"), stats)
        self.assertEqual((stats.files, stats.bytes), (2, 15))
        self.assertFalse(os.path.exists(os.path.join(self.root, "junk")))

    def test_cutoff_keeps_recent_files(self):
        old = self.write("junk/old.tmp", age_hours=5)
        new = self.write("junk/new.tmp")
        stats = Stats()
        fsutil.remove(os.path.join(self.root, "junk"), stats, cutoff=time.time() - 3600)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(new))
        self.assertEqual(stats.files, 1)

    def test_junction_is_not_followed(self):
        precious = self.write("precious/keep.txt")
        os.makedirs(os.path.join(self.root, "junk"))
        link = os.path.join(self.root, "junk", "link")
        subprocess.run(["cmd", "/c", "mklink", "/J", link, os.path.join(self.root, "precious")],
                       check=True, capture_output=True)
        fsutil.remove(os.path.join(self.root, "junk"), Stats())
        self.assertFalse(os.path.lexists(link))
        self.assertTrue(os.path.exists(precious))

    def test_protected_paths_refused(self):
        for path in (os.environ["SystemRoot"], "C:\\", os.environ["USERPROFILE"],
                     os.path.join(os.environ["USERPROFILE"], "Documents"), os.environ["TEMP"]):
            self.assertFalse(fsutil.is_safe(path), path)
        self.assertTrue(fsutil.is_safe(os.path.join(os.environ["TEMP"], "whatever.tmp")))


class FirefoxPlacesTests(unittest.TestCase):
    def test_history_cleared_bookmarks_kept(self):
        tmp = tempfile.mkdtemp(prefix="pccleaner_ff_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        db = os.path.join(tmp, "places.sqlite")
        con = sqlite3.connect(db)
        con.executescript("""
            CREATE TABLE moz_origins (id INTEGER PRIMARY KEY, host TEXT);
            CREATE TABLE moz_places (id INTEGER PRIMARY KEY, url TEXT, visit_count INT,
                                     last_visit_date INT, foreign_count INT DEFAULT 0, origin_id INT);
            CREATE TABLE moz_historyvisits (id INTEGER PRIMARY KEY, place_id INT);
            CREATE TABLE moz_bookmarks (id INTEGER PRIMARY KEY, fk INT);
            CREATE TABLE moz_inputhistory (place_id INT, input TEXT);
            CREATE TABLE moz_annos (id INTEGER PRIMARY KEY, place_id INT);
            INSERT INTO moz_origins VALUES (1, 'bookmarked.example'), (2, 'visited.example');
            INSERT INTO moz_places VALUES (1, 'https://bookmarked.example', 3, 1, 1, 1),
                                          (2, 'https://visited.example', 5, 1, 0, 2);
            INSERT INTO moz_historyvisits (place_id) VALUES (1), (1), (2), (2), (2);
            INSERT INTO moz_bookmarks (fk) VALUES (1);
            INSERT INTO moz_inputhistory VALUES (2, 'vis');
            INSERT INTO moz_annos (place_id) VALUES (2);
        """)
        con.commit()
        con.close()

        self.assertEqual(browsers.count_places_history(db), 5)
        self.assertEqual(browsers.clear_places_history(db), 5)

        con = sqlite3.connect(db)
        self.assertEqual(con.execute("SELECT url, visit_count FROM moz_places").fetchall(),
                         [("https://bookmarked.example", 0)])
        self.assertEqual(con.execute("SELECT COUNT(*) FROM moz_bookmarks").fetchone()[0], 1)
        for table in ("moz_historyvisits", "moz_inputhistory", "moz_annos"):
            self.assertEqual(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)
        self.assertEqual(con.execute("SELECT host FROM moz_origins").fetchall(), [("bookmarked.example",)])
        con.close()


class VlcTests(unittest.TestCase):
    def test_recent_list_removed_other_settings_kept(self):
        tmp = tempfile.mkdtemp(prefix="pccleaner_vlc_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        ini = os.path.join(tmp, "vlc-qt-interface.ini")
        with open(ini, "w", encoding="utf-8", newline="") as f:
            f.write("[MainWindow]\r\nplaylist-visible=false\r\n"
                    "[RecentsMRL]\r\nlist=file:///C:/a.mp4, \"file:///C:/b,c.mkv\", file:///C:/d.mp3\r\n"
                    "times=0, 0, 0\r\n"
                    "[OpenDialog]\r\nnetMRU=http://stream.example/live\r\n")
        self.assertEqual(media.vlc_scan(ini).entries, 4)
        self.assertEqual(media.vlc_clean(ini).entries, 4)
        self.assertEqual(media.vlc_scan(ini).entries, 0)
        with open(ini, encoding="utf-8", newline="") as f:
            text = f.read()
        self.assertIn("playlist-visible=false\r\n", text)
        self.assertNotIn("a.mp4", text)


class AppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pccleaner_apps_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_notepadpp_history_removed_settings_kept(self):
        cfg = os.path.join(self.tmp, "config.xml")
        with open(cfg, "w", encoding="utf-8", newline="") as f:
            f.write('<?xml version="1.0" encoding="UTF-8" ?>\r\n<NotepadPlus>\r\n'
                    '    <FindHistory nbMaxFindHistoryPath="10" matchCase="no">\r\n'
                    '        <Path name="C:\\src" />\r\n        <Find name="secret" />\r\n'
                    '        <Replace name="other" />\r\n    </FindHistory>\r\n'
                    '    <History nbMaxFile="10" inSubMenu="no">\r\n'
                    '        <File filename="C:\\a.txt" />\r\n        <File filename="C:\\b.txt" />\r\n'
                    '    </History>\r\n    <GUIConfig name="TabBar">yes</GUIConfig>\r\n</NotepadPlus>\r\n')
        self.assertEqual(apps.npp_scan(cfg).entries, 5)
        self.assertEqual(apps.npp_clean(cfg).entries, 5)
        self.assertEqual(apps.npp_scan(cfg).entries, 0)
        with open(cfg, encoding="utf-8", newline="") as f:
            text = f.read()
        self.assertIn('<History nbMaxFile="10" inSubMenu="no">\r\n    </History>', text)
        self.assertIn('matchCase="no"', text)
        self.assertIn('<GUIConfig name="TabBar">yes</GUIConfig>', text)
        self.assertNotIn("secret", text)

    def test_notepadpp_empty_self_closing_history(self):
        cfg = os.path.join(self.tmp, "config.xml")
        with open(cfg, "w", encoding="utf-8") as f:
            f.write('<NotepadPlus><FindHistory nbMaxFindHistoryPath="10" /><History nbMaxFile="10">'
                    '<File filename="C:\\a.txt" /></History></NotepadPlus>')
        self.assertEqual(apps.npp_scan(cfg).entries, 1)

    def test_vscode_recent_list_cleared(self):
        db = os.path.join(self.tmp, "state.vscdb")
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")
        con.execute("INSERT INTO ItemTable VALUES (?, ?)", (apps.VSCODE_RECENT_KEY, json.dumps(
            {"entries": [{"folderUri": "file:///c%3A/x"}, {"fileUri": "file:///c%3A/y.py"}]})))
        con.execute("INSERT INTO ItemTable VALUES ('workbench.theme', 'dark')")
        con.commit()
        con.close()
        self.assertEqual(apps._vscode_recent_count(db), 2)
        self.assertEqual(apps._vscode_recent_clear(db), 2)
        self.assertEqual(apps._vscode_recent_count(db), 0)
        con = sqlite3.connect(db)
        self.assertEqual(con.execute("SELECT value FROM ItemTable WHERE key='workbench.theme'").fetchone()[0], "dark")
        con.close()


class RegistryTests(unittest.TestCase):
    def test_tree_count_and_find_keys(self):
        path = rf"Software\PCCleanerTest_{uuid.uuid4().hex}"
        self.addCleanup(lambda: regutil._delete_tree(winreg.HKEY_CURRENT_USER, path)
                        if regutil.key_exists(path) else None)
        for sub in (r"\16.0\Word\File MRU", r"\16.0\Word\User MRU\LiveId_1\File MRU", r"\16.0\Excel\Place MRU"):
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path + sub) as k:
                winreg.SetValueEx(k, "Item 1", 0, winreg.REG_SZ, "x")
        found = regutil.find_keys(path + r"\16.0", {"file mru", "place mru"})  # as apps.py searches
        self.assertEqual(len(found), 3)
        self.assertEqual(regutil.count_entries(path, "tree"), 8)
        for key in found:
            regutil.clear_values(key)
        self.assertEqual(sum(len(regutil.value_names(k)) for k in found), 0)
        self.assertTrue(all(regutil.key_exists(k) for k in found))

    def test_clear_mru_key(self):
        path = rf"Software\PCCleanerTest_{uuid.uuid4().hex}"
        self.addCleanup(lambda: regutil._delete_tree(winreg.HKEY_CURRENT_USER, path)
                        if regutil.key_exists(path) else None)
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path) as k:
            for name in "abc":
                winreg.SetValueEx(k, name, 0, winreg.REG_SZ, f"cmd {name}")
            winreg.SetValueEx(k, "MRUList", 0, winreg.REG_SZ, "abc")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path + r"\.txt") as k:
            winreg.SetValueEx(k, "0", 0, winreg.REG_BINARY, b"\x00")

        self.assertEqual(regutil.count_entries(path), 3)
        regutil.clear(path)
        self.assertTrue(regutil.key_exists(path))
        self.assertEqual(regutil.count_entries(path), 0)
        self.assertFalse(regutil.key_exists(path + r"\.txt"))


class CatalogTests(unittest.TestCase):
    def test_ids_unique(self):
        ids = [t.id for t in build_catalog()]
        self.assertEqual(len(ids), len(set(ids)))


if __name__ == "__main__":
    unittest.main()
