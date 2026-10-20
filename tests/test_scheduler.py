"""Scheduler tests: XML generation, unattended runs (mocked catalog) and reports."""
import os
import shutil
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

from pccleaner import report, scheduler, settings
from pccleaner.core import Custom, Target
from pccleaner.stats import Stats

if sys.platform == "win32":
    from pccleaner.platform import windows

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


@unittest.skipUnless(sys.platform == "win32", "Windows Task Scheduler")
class TaskXmlTests(unittest.TestCase):
    def parse(self, xml):
        return ET.fromstring(xml.split("?>", 1)[1])

    def test_weekly(self):
        root = self.parse(windows.task_xml("weekly", "21:30", "Sunday"))
        trigger = root.find("t:Triggers/t:CalendarTrigger", NS)
        self.assertTrue(trigger.find("t:StartBoundary", NS).text.endswith("T21:30:00"))
        self.assertIsNotNone(trigger.find("t:ScheduleByWeek/t:DaysOfWeek/t:Sunday", NS))
        self.assertEqual(root.find("t:Settings/t:StartWhenAvailable", NS).text, "true")
        self.assertEqual(root.find("t:Settings/t:DisallowStartIfOnBatteries", NS).text, "false")
        self.assertEqual(root.find("t:Principals/t:Principal/t:RunLevel", NS).text, "LeastPrivilege")
        exec_ = root.find("t:Actions/t:Exec", NS)
        self.assertIn("scheduled-run", exec_.find("t:Arguments", NS).text)
        self.assertTrue(os.path.isdir(exec_.find("t:WorkingDirectory", NS).text))

    def test_daily_elevated(self):
        root = self.parse(windows.task_xml("daily", "07:05", elevated=True))
        self.assertIsNotNone(root.find("t:Triggers/t:CalendarTrigger/t:ScheduleByDay", NS))
        self.assertEqual(root.find("t:Principals/t:Principal/t:RunLevel", NS).text, "HighestAvailable")

    def test_bad_input(self):
        with self.assertRaises(ValueError):
            windows.task_xml("hourly", "10:00")
        with self.assertRaises(ValueError):
            windows.task_xml("weekly", "10:00", "Funday")


class ScheduledRunTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="pccleaner_sched_")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        patches = [mock.patch.object(settings, "SETTINGS_FILE", os.path.join(self.dir, "settings.json")),
                   mock.patch.object(settings, "APP_DIR", self.dir),
                   mock.patch.object(report, "LOG_DIR", os.path.join(self.dir, "logs"))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.cleaned = []

    def target(self, tid, **kw):
        return Target(tid, "Test", tid, "", [Custom(Stats, lambda: self.cleaned.append(tid) or Stats(files=2, bytes=10))],
                      **kw)

    def test_runs_only_scheduled_and_allowed_items(self):
        catalog = [self.target("a"), self.target("b"), self.target("busy", processes=("editor.exe",)),
                   self.target("adm", admin=True), self.target("not_scheduled")]
        settings.update(checked={"x": True}, schedule={"items": ["a", "b", "busy", "adm", "gone"]})
        with mock.patch("pccleaner.cleaners.build_catalog", return_value=catalog), \
                mock.patch.object(scheduler.plat, "is_admin", return_value=False), \
                mock.patch.object(scheduler.plat, "running_processes", return_value={"editor.exe"}):
            path = scheduler.run_scheduled()

        self.assertEqual(self.cleaned, ["a", "b"])
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("Total: 20 B freed, 4 files", text)
        self.assertIn("editor.exe was running", text)
        self.assertIn("needs administrator rights", text)
        self.assertIn("gone: no longer available", text)
        self.assertEqual(settings.load()["checked"], {"x": True})  # other settings untouched

    def test_no_schedule_cleans_nothing(self):
        with mock.patch("pccleaner.cleaners.build_catalog", return_value=[self.target("a")]):
            scheduler.run_scheduled()
        self.assertEqual(self.cleaned, [])


if __name__ == "__main__":
    unittest.main()
