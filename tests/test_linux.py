"""Linux port tests: the catalog against a throwaway home folder, the platform layer, safety rules."""
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

if not sys.platform.startswith("linux"):
    raise unittest.SkipTest("Linux")

from pccleaner import fsutil  # noqa: E402
from pccleaner.cleaners import build_catalog, linux  # noqa: E402
from pccleaner.platform import linux as plat  # noqa: E402

OLD = time.time() - 3 * 3600  # older than the 1-hour safety margin


class FakeHome(unittest.TestCase):
    """Each test gets its own HOME; XDG variables are unset so the defaults under HOME apply."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="pccleaner_home_")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        env = {k: v for k, v in os.environ.items() if not k.startswith("XDG_")}
        env["HOME"] = self.home
        patch = mock.patch.dict(os.environ, env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        fsutil._protected.cache_clear()
        self.addCleanup(fsutil._protected.cache_clear)

    def file(self, rel, content=b"x" * 100, mtime=OLD):
        path = os.path.join(self.home, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(content)
        os.utime(path, (mtime, mtime))
        return path

    def target(self, tid):
        found = {t.id: t for t in build_catalog()}
        self.assertIn(tid, found, f"{tid} not in {sorted(found)}")
        return found[tid]


class CatalogTests(FakeHome):
    def test_app_caches_keep_recent_files_and_items_with_their_own_entry(self):
        old = self.file(".cache/someapp/old.bin")
        new = self.file(".cache/someapp/new.bin", mtime=time.time())
        thumb = self.file(".cache/thumbnails/large/a.png")
        t = self.target("linux.app_cache")
        self.assertEqual(t.scan().files, 1)
        t.clean()
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(new))      # changed in the last hour
        self.assertTrue(os.path.exists(thumb))    # thumbnails have their own (unticked) item
        self.assertFalse(self.target("linux.thumbnails").default)

    def test_flatpak_app_caches(self):
        cached = self.file(".var/app/org.gnome.Maps/cache/tiles/1.png")
        firefox = self.file(".var/app/org.mozilla.firefox/cache/x.bin")
        self.target("linux.flatpak_cache").clean()
        self.assertFalse(os.path.exists(cached))
        self.assertTrue(os.path.exists(firefox))  # Firefox's own item handles it

    def test_trash_is_unticked_and_emptied_on_request(self):
        self.file(".local/share/Trash/files/report.pdf")
        self.file(".local/share/Trash/info/report.pdf.trashinfo")
        t = self.target("linux.trash")
        self.assertFalse(t.default)
        self.assertEqual(t.clean().files, 2)
        self.assertTrue(os.path.isdir(os.path.join(self.home, ".local/share/Trash/files")))

    def test_recent_files_and_shell_history(self):
        recent = self.file(".local/share/recently-used.xbel")
        bash = self.file(".bash_history")
        self.target("linux.recent_files").clean()
        self.assertFalse(os.path.exists(recent))
        shell = self.target("linux.shell_history")
        self.assertFalse(shell.default)
        self.assertTrue(shell.warning)
        shell.clean()
        self.assertFalse(os.path.exists(bash))

    def _places(self, profile_dir):
        os.makedirs(profile_dir, exist_ok=True)
        con = sqlite3.connect(os.path.join(profile_dir, "places.sqlite"))
        con.executescript("""
            CREATE TABLE moz_places (id INTEGER PRIMARY KEY, url TEXT, visit_count INT,
                                     last_visit_date INT, foreign_count INT DEFAULT 0);
            CREATE TABLE moz_historyvisits (id INTEGER PRIMARY KEY, place_id INT);
            CREATE TABLE moz_bookmarks (id INTEGER PRIMARY KEY, fk INT);
            INSERT INTO moz_places VALUES (1, 'https://kept.example', 2, 1, 1), (2, 'https://gone.example', 4, 1, 0);
            INSERT INTO moz_historyvisits (place_id) VALUES (1), (1), (2), (2), (2), (2);
            INSERT INTO moz_bookmarks (fk) VALUES (1);
        """)
        con.commit()
        con.close()

    def test_flatpak_firefox_history_cleared_bookmarks_kept(self):
        root = os.path.join(self.home, ".var/app/org.mozilla.firefox/.mozilla/firefox")
        self.file(".var/app/org.mozilla.firefox/.mozilla/firefox/profiles.ini",
                  b"[Profile0]\nName=default\nIsRelative=1\nPath=abc.default-release\n")
        self._places(os.path.join(root, "abc.default-release"))
        cached = self.file(".var/app/org.mozilla.firefox/cache/mozilla/firefox/abc.default-release/cache2/entries/1")

        history = self.target("firefox.history")
        self.assertIn("firefox", history.processes)
        self.assertEqual(history.scan().entries, 6)
        history.clean()
        con = sqlite3.connect(os.path.join(root, "abc.default-release", "places.sqlite"))
        self.assertEqual([r[0] for r in con.execute("SELECT url FROM moz_places")], ["https://kept.example"])
        self.assertEqual(con.execute("SELECT COUNT(*) FROM moz_historyvisits").fetchone()[0], 0)
        con.close()
        self.target("firefox.cache").clean()
        self.assertFalse(os.path.exists(cached))

    def test_native_and_flatpak_chrome_profiles(self):
        native = self.file(".config/google-chrome/Default/History")
        native_cache = self.file(".cache/google-chrome/Default/Cache/Cache_Data/f_0001")
        flat = self.file(".var/app/com.google.Chrome/config/google-chrome/Profile 1/History")
        self.target("chrome.history").clean()
        self.target("chrome.cache").clean()
        self.target("chrome_flatpak.history").clean()
        self.assertFalse(os.path.exists(native) or os.path.exists(native_cache) or os.path.exists(flat))
        self.assertFalse(self.target("chrome.cookies").default)

    def test_vlc_recent_list_in_flatpak_config(self):
        ini = self.file(".var/app/org.videolan.VLC/config/vlc/vlc-qt-interface.ini",
                        b"[General]\nqt-volume=1\n[RecentsMRL]\nlist=file:///a.mp4, file:///b.mkv\ntimes=0, 0\n")
        t = self.target("media.vlc")
        self.assertEqual(t.scan().entries, 2)
        t.clean()
        with open(ini, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("qt-volume=1", text)
        self.assertNotIn("a.mp4", text)

    def test_only_installed_things_are_listed(self):
        ids = {t.id for t in build_catalog()}
        self.assertNotIn("firefox.history", ids)
        self.assertNotIn("media.vlc", ids)
        self.assertNotIn("dev.npm", ids)
        self.assertIn("linux.app_cache", ids)

    def test_maintenance_items_only_with_the_system_helper(self):
        with mock.patch.object(linux, "HELPER", "/nonexistent/helper"):
            self.assertNotIn("linux.journal", {t.id for t in build_catalog()})
        helper = self.file("helper", b"#!/bin/sh\n")
        with mock.patch.object(linux, "HELPER", helper):
            journal = self.target("linux.journal")
        self.assertTrue(journal.approval)
        self.assertFalse(journal.default)

    def test_approval_cancelled_is_reported_not_raised(self):
        with mock.patch.object(linux, "_helper", return_value=subprocess.CompletedProcess([], 126, "", "")):
            stats = linux._coredump_clean()
        self.assertIn("approval was cancelled", " ".join(stats.messages))

    def test_journal_sizes(self):
        self.assertEqual(linux.parse_size("1.5G"), int(1.5 * 1024 ** 3))
        self.assertEqual(linux.parse_size("24.0M"), 24 * 1024 ** 2)
        self.assertEqual(linux.parse_size("512B"), 512)


class SafetyTests(FakeHome):
    def test_home_and_its_main_folders_are_refused(self):
        for rel in ("", ".config", ".cache", "Documents", ".var/app"):
            path = os.path.join(self.home, rel)
            os.makedirs(path, exist_ok=True)
            self.assertFalse(fsutil.is_safe(path), rel)
        for path in ("/", "/usr", "/etc", "/var/home"):
            self.assertFalse(fsutil.is_safe(path), path)
        self.assertTrue(fsutil.is_safe(os.path.join(self.home, ".cache", "someapp")))

    def test_links_are_removed_not_followed(self):
        precious = self.file("Documents/keep.txt")
        cache = os.path.join(self.home, ".cache", "app")
        os.makedirs(cache)
        os.symlink(os.path.dirname(precious), os.path.join(cache, "docs-link"))
        self.target("linux.app_cache").clean()
        self.assertTrue(os.path.exists(precious))
        self.assertFalse(os.path.lexists(os.path.join(cache, "docs-link")))


class PlatformTests(FakeHome):
    def test_folders_follow_xdg(self):
        self.assertEqual(plat.app_dir(), os.path.join(self.home, ".config", "pccleaner"))
        self.assertEqual(plat.log_dir(), os.path.join(self.home, ".local", "state", "pccleaner", "logs"))

    def test_timer_units(self):
        service, timer = plat.unit_files("weekly", "21:30", "Sunday")
        self.assertIn("OnCalendar=Sun *-*-* 21:30:00", timer)
        self.assertIn("Persistent=true", timer)  # a missed run happens at the next start
        self.assertIn("scheduled-run", service)
        _, daily = plat.unit_files("daily", "7:05")
        self.assertIn("OnCalendar=*-*-* 07:05:00", daily)
        with self.assertRaises(ValueError):
            plat.unit_files("weekly", "10:00", "Funday")
        with self.assertRaises(ValueError):
            plat.unit_files("hourly", "10:00")

    def test_processes_found_and_closed_politely(self):
        sleeper = os.path.join(self.home, "pcc-test-sleeper")
        shutil.copy(shutil.which("sleep"), sleeper)
        proc = subprocess.Popen([sleeper, "300"])
        self.addCleanup(proc.kill)
        time.sleep(0.3)
        self.assertIn("pcc-test-sleeper", plat.running_processes())
        self.assertEqual(plat.close_processes(["pcc-test-sleeper"], timeout=5), set())
        # It got SIGTERM (a polite close); wait for it to finish exiting rather than racing it
        self.assertEqual(proc.wait(timeout=5), -signal.SIGTERM)

    def test_never_runs_as_admin_itself(self):
        self.assertFalse(plat.relaunch_as_admin())


if __name__ == "__main__":
    unittest.main()
