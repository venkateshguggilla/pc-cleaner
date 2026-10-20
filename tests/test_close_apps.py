"""Closing running programs before cleaning.

Process tests only touch processes they start themselves (by PID), never real programs by name.
"""
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

from pccleaner import winutil
from pccleaner.core import Custom, Target
from pccleaner.stats import Stats

PYTHONW = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")


def spawn(code: str, window: bool) -> subprocess.Popen:
    exe = PYTHONW if window else sys.executable
    flags = 0 if window else subprocess.CREATE_NO_WINDOW
    return subprocess.Popen([exe, "-c", code], creationflags=flags)


class ProcessTests(unittest.TestCase):
    def test_graceful_close_of_windowed_process(self):
        proc = spawn("import tkinter as t; r = t.Tk(); r.title('pccleaner close test'); r.mainloop()", window=True)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        time.sleep(2.5)  # let the window appear
        self.assertGreaterEqual(winutil.close_windows([proc.pid]), 1)
        self.assertEqual(winutil.wait_for_exit([proc.pid], 10), set())
        self.assertEqual(proc.wait(5), 0)  # exited normally, not killed

    def test_windowless_process_needs_force(self):
        proc = spawn("import time; time.sleep(120)", window=False)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        time.sleep(0.5)
        self.assertEqual(winutil.close_windows([proc.pid]), 0)
        self.assertEqual(winutil.wait_for_exit([proc.pid], 1), {proc.pid})
        winutil.kill_pids([proc.pid])
        self.assertEqual(winutil.wait_for_exit([proc.pid], 10), set())

    def test_find_pids_never_returns_self(self):
        me = os.path.basename(sys.executable).lower()
        self.assertNotIn(os.getpid(), winutil.find_pids([me]))


class ShortcutTests(unittest.TestCase):
    def test_create_shortcut_round_trip(self):
        import tempfile
        folder = tempfile.mkdtemp(prefix="pccleaner_lnk_")
        self.addCleanup(lambda: __import__("shutil").rmtree(folder, ignore_errors=True))
        lnk = os.path.join(folder, "sub", "PC Cleaner.lnk")
        project = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        winutil.create_shortcut(lnk, PYTHONW, "-m pccleaner", project,
                                os.path.join(project, "assets", "pccleaner.ico"), "PC Cleaner")
        out = winutil.run_powershell(
            f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}'); "
            "$s.TargetPath; $s.Arguments; $s.WorkingDirectory; $s.IconLocation; $s.Description").stdout
        target, args, workdir, icon, desc = out.strip().splitlines()
        self.assertEqual(os.path.normcase(target), os.path.normcase(PYTHONW))
        self.assertEqual(args, "-m pccleaner")
        self.assertEqual(os.path.normcase(workdir), os.path.normcase(project))
        self.assertTrue(icon.lower().endswith("pccleaner.ico,0"))
        self.assertEqual(desc, "PC Cleaner")


class CleanFlowTests(unittest.TestCase):
    """Run Cleaner with a program open: close it (or not), then clean."""

    def setUp(self):
        self.cleaned = []

        def target(tid, procs=()):
            return Target(tid, "Test", tid, "", default=True, processes=procs,
                          parts=[Custom(Stats, lambda: self.cleaned.append(tid) or Stats(files=1))])

        catalog = [target("browser", ("browser.exe",)), target("temp")]
        patches = [mock.patch("pccleaner.gui.build_catalog", return_value=catalog),
                   mock.patch("pccleaner.gui.settings.load", return_value={}),
                   mock.patch("pccleaner.gui.settings.update"),
                   mock.patch("pccleaner.gui.report.write", return_value="report.log"),
                   mock.patch("pccleaner.gui.plat.running_processes", return_value={"browser.exe"})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        from pccleaner import gui
        self.gui = gui
        self.app = gui.App()
        self.addCleanup(self.app.destroy)

    def run_until_idle(self, timeout=10):
        deadline = time.monotonic() + timeout
        self.app.update()
        while time.monotonic() < deadline:
            self.app.update()
            if not self.app.working and self.app.events.empty():
                time.sleep(0.2)
                self.app.update()
                if not self.app.working and self.app.events.empty():
                    return
            time.sleep(0.05)
        self.fail("cleaning did not finish")

    def start(self, close_answer, force_answer=None, closes=True, force_works=True):
        asks = {"Close programs?": close_answer, "Confirm cleaning": True, "Force close?": force_answer}
        with mock.patch.object(self.gui.messagebox, "askyesnocancel", side_effect=lambda t, *a, **k: asks[t]), \
                mock.patch.object(self.gui.messagebox, "askyesno", side_effect=lambda t, *a, **k: asks[t]), \
                mock.patch.object(self.gui.plat, "close_processes",
                                  return_value=set() if closes else {"browser.exe"}) as close, \
                mock.patch.object(self.gui.plat, "kill_processes",
                                  return_value=set() if force_works else {"browser.exe"}) as kill:
            self.app._start("clean")
            self.run_until_idle()
        return close, kill

    def test_yes_closes_program_and_cleans_everything(self):
        close, kill = self.start(close_answer=True)
        close.assert_called_once_with(["browser.exe"])
        kill.assert_not_called()
        self.assertEqual(sorted(self.cleaned), ["browser", "temp"])

    def test_no_skips_the_programs_items(self):
        close, _ = self.start(close_answer=False)
        close.assert_not_called()
        self.assertEqual(self.cleaned, ["temp"])

    def test_cancel_cleans_nothing(self):
        self.start(close_answer=None)
        self.assertEqual(self.cleaned, [])

    def test_force_close_when_graceful_close_fails(self):
        _, kill = self.start(close_answer=True, closes=False, force_answer=True)
        kill.assert_called_once_with({"browser.exe"})
        self.assertEqual(sorted(self.cleaned), ["browser", "temp"])

    def test_declining_force_close_skips_its_items(self):
        _, kill = self.start(close_answer=True, closes=False, force_answer=False)
        kill.assert_not_called()
        self.assertEqual(self.cleaned, ["temp"])


if __name__ == "__main__":
    unittest.main()
