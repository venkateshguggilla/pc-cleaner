"""winapp2.ini engine tests - rules only point at a temp folder and a throwaway HKCU key."""
import sys
import unittest

if sys.platform != "win32":
    raise unittest.SkipTest("Windows: registry, Windows processes or the Recycle Bin")

import os
import shutil
import tempfile
import time
import unittest
import uuid
import winreg

from pccleaner import regutil
from pccleaner.cleaners import winapp2


class Winapp2Tests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="pccleaner_w2_")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.var_root = "%Temp%\\" + os.path.basename(self.root)  # how a rule would reference it
        self.reg = rf"Software\PCCleanerTest_{uuid.uuid4().hex}"
        self.addCleanup(lambda: regutil.delete_key(self.reg) if regutil.key_exists(self.reg) else None)

    def write(self, rel):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("data")
        return path

    def targets(self, ini_text, min_age_hours=0):
        ini = os.path.join(self.root, "winapp2.ini")
        with open(ini, "w", encoding="utf-8") as f:
            f.write(ini_text)
        return {t.name: t for t in winapp2.targets(ini, min_age_hours)}

    def test_recent_files_kept_by_default(self):
        fresh = self.write(r"t\fresh.tmp")
        old = self.write(r"t\old.tmp")
        two_hours_ago = time.time() - 7200
        os.utime(old, (two_hours_ago, two_hours_ago))
        t = self.targets(f"[T *]\nFileKey1={self.var_root}\\t|*.tmp\n",
                         min_age_hours=winapp2.MIN_AGE_HOURS)["T"]
        self.assertEqual(t.clean().files, 1)
        self.assertTrue(os.path.exists(fresh))
        self.assertFalse(os.path.exists(old))

    def test_file_associations_key_protected(self):
        t = self.targets("[Shell *]\nRegKey1=HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\FileExts\n")
        self.assertTrue(any("core key" in m for m in t["Shell"].scan().messages))

    def test_parse_and_detection(self):
        found = self.targets(f"""; comment
[Present App *]
LangSecRef=3021
DetectFile={self.var_root}
FileKey1={self.var_root}\\cache|*.*
[Missing App *]
Detect=HKCU\\Software\\NoSuchVendor_{uuid.uuid4().hex}
DetectFile=%LocalAppData%\\NoSuchFolder_{uuid.uuid4().hex}
FileKey1=%LocalAppData%\\NoSuchFolder|*.*
[Future Windows Only *]
DetectOS=99.0|
FileKey1={self.var_root}|*.tmp
[No Detect Keys *]
Section=My Section
FileKey1={self.var_root}\\nothing|*.*
""")
        self.assertEqual(set(found), {"Present App", "No Detect Keys"})
        self.assertEqual(found["Present App"].group, "winapp2: Applications")
        self.assertEqual(found["No Detect Keys"].group, "winapp2: My Section")
        self.assertFalse(found["Present App"].default)

    def test_file_rules_excludes_and_removeself(self):
        top_log = self.write(r"app\a.log")
        top_txt = self.write(r"app\keep.txt")
        deep_log = self.write(r"app\sub\b.log")
        kept_by_file = self.write(r"app\important.log")
        cache_files = [self.write(r"cache\x\1.bin"), self.write(r"cache\x\y\2.bin")]
        kept_by_path = self.write(r"cache\keepdir\3.bin")
        t = self.targets(f"""[Rules *]
FileKey1={self.var_root}\\app|*.log;*.dmp
FileKey2={self.var_root}\\cache|*.*|REMOVESELF
ExcludeKey1=FILE|{self.var_root}\\app\\|important.log
ExcludeKey2=PATH|{self.var_root}\\cache\\keepdir\\
""")["Rules"]

        self.assertEqual(t.scan().files, 3)  # a.log + two cache files
        stats = t.clean()
        self.assertEqual(stats.files, 3)
        self.assertFalse(os.path.exists(top_log))
        for kept in (top_txt, deep_log, kept_by_file, kept_by_path):  # deep_log: no RECURSE
            self.assertTrue(os.path.exists(kept), kept)
        self.assertFalse(os.path.exists(os.path.join(self.root, r"cache\x")))  # REMOVESELF removed empty dirs
        self.assertTrue(os.path.isdir(os.path.join(self.root, r"cache\keepdir")))

    def test_recurse_and_wildcard_folders(self):
        logs = [self.write(r"profiles\p1\logs\a.log"), self.write(r"profiles\p2\logs\deep\b.log")]
        other = self.write(r"profiles\p1\data.db")
        t = self.targets(f"[W *]\nFileKey1={self.var_root}\\profiles\\*\\logs|*.log|RECURSE\n")["W"]
        self.assertEqual(t.clean().files, 2)
        self.assertFalse(any(map(os.path.exists, logs)))
        self.assertTrue(os.path.exists(other))

    def test_registry_rules(self):
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self.reg + r"\Recent\Sub") as k:
            winreg.SetValueEx(k, "x", 0, winreg.REG_SZ, "1")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self.reg + r"\Settings") as k:
            winreg.SetValueEx(k, "LastPath", 0, winreg.REG_SZ, "C:\\secret")
            winreg.SetValueEx(k, "Theme", 0, winreg.REG_SZ, "dark")
        t = self.targets(f"""[Reg *]
RegKey1=HKCU\\{self.reg}\\Recent
RegKey2=HKCU\\{self.reg}\\Settings|LastPath
RegKey3=HKLM\\Software\\SomethingGlobal
RegKey4=HKCU\\Software\\Microsoft
""")["Reg"]
        scan = t.scan()
        self.assertEqual(scan.entries, 2)
        self.assertTrue(any("outside HKCU" in m for m in scan.messages))
        self.assertTrue(any("core key" in m for m in scan.messages))
        t.clean()
        self.assertFalse(regutil.key_exists(self.reg + r"\Recent"))
        self.assertIsNone(regutil.get_value(self.reg + r"\Settings", "LastPath"))
        self.assertEqual(regutil.get_value(self.reg + r"\Settings", "Theme"), "dark")
        self.assertTrue(regutil.key_exists(r"Software\Microsoft"))

    def test_dangerous_rules_refused(self):
        t = self.targets("[Bad *]\nFileKey1=%UserProfile%|*.*|RECURSE\nFileKey2=%WinDir%|*.*\n")["Bad"]
        stats = t.scan()
        self.assertEqual(stats.files, 0)
        self.assertEqual(len([m for m in stats.messages if "unsafe rule" in m]), 2)

    def test_warning_is_kept(self):
        self.write(r"w\f.tmp")
        t = self.targets(f"[Warn *]\nWarning=This removes saved sessions.\nFileKey1={self.var_root}\\w|*.tmp\n")["Warn"]
        self.assertEqual(t.warning, "This removes saved sessions.")


if __name__ == "__main__":
    unittest.main()
