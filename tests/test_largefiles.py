"""Large file finder tests (temp folders only)."""
import sys
import unittest

if sys.platform != "win32":
    raise unittest.SkipTest("Windows: registry, Windows processes or the Recycle Bin")

import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest

from pccleaner import largefiles, winutil

MB = 1024 * 1024


class FindTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="pccleaner_large_")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def make(self, rel, size, age_days=0):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.truncate(size)
        if age_days:
            t = time.time() - age_days * 86400
            os.utime(path, (t, t))
        return path

    def test_finds_biggest_first_with_filters(self):
        big = self.make(r"a\big.iso", 30 * MB, age_days=400)
        mid = self.make(r"a\b\mid.mp4", 12 * MB)
        self.make(r"small.txt", 1 * MB)
        found = largefiles.find(self.root, 10 * MB)
        self.assertEqual([f.path for f in found], [big, mid])
        self.assertEqual(found[0].size, 30 * MB)
        self.assertEqual([f.path for f in largefiles.find(self.root, 10 * MB, min_age_days=365)], [big])

    def test_junction_not_followed(self):
        outside = tempfile.mkdtemp(prefix="pccleaner_large_out_")
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        with open(os.path.join(outside, "elsewhere.bin"), "wb") as f:
            f.truncate(20 * MB)
        subprocess.run(["cmd", "/c", "mklink", "/J", os.path.join(self.root, "link"), outside],
                       check=True, capture_output=True)
        self.assertEqual(largefiles.find(self.root, MB), [])

    def test_cancel_and_progress(self):
        self.make(r"x\f.bin", 2 * MB)
        cancel = threading.Event()
        cancel.set()
        self.assertEqual(largefiles.find(self.root, MB, cancel=cancel), [])
        seen = []
        largefiles.find(self.root, MB, progress=lambda folders, matched: seen.append((folders, matched)))
        self.assertEqual(seen[-1], (2, 1))

    def test_app_file_detection(self):
        profile, local = os.environ["USERPROFILE"], os.environ["LOCALAPPDATA"]
        self.assertTrue(largefiles.is_app_file(os.path.join(local, r"Android\Sdk\system.img")))
        self.assertTrue(largefiles.is_app_file(os.path.join(profile, r".android\avd\ram.img")))
        self.assertFalse(largefiles.is_app_file(os.path.join(profile, r"Downloads\ubuntu.iso")))
        self.assertFalse(largefiles.is_app_file(os.path.join(profile, r"Videos\.trip.mp4")))  # dot-file, not folder

    def test_shell_delete_struct(self):
        # Exercises the SHFileOperation marshalling without touching the real Recycle Bin:
        # a permanent delete of two temp files (no FOF_ALLOWUNDO).
        paths = [self.make("one.tmp", 10), self.make("two words.tmp", 10)]
        flags = winutil.FOF_NOCONFIRMATION | winutil.FOF_NOERRORUI | 0x4  # 0x4 = FOF_SILENT
        self.assertEqual(winutil._shell_delete(paths, flags), 0)
        self.assertFalse(any(map(os.path.exists, paths)))


if __name__ == "__main__":
    unittest.main()
