import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from moodlectl import render, sync  # noqa: E402
from moodlectl import main as cli  # noqa: E402

ASSIGN_HTML = """<html><body><div id="region-main">
<div data-region="activity-dates" class="activity-dates"><div><strong>Opened:</strong> Saturday, 21 March 2026, 12:00 AM</div>
<div><strong>Due:</strong> Friday, 30 October 2026, 5:00 PM</div></div>
<div class="sr-only">hidden</div><p>Submit the <b>proposal</b>.</p>
<a href="https://moodle.example/pluginfile.php/1/mod_assign/introattachment/0/Brief%202026.pdf?forcedownload=1">Brief 2026.pdf</a>
</div></body></html>"""

SECTION_HTML = """<html><body><div id="region-main"><h2>Section outline</h2>
<ul class="section"><li class="activity" data-for="cmitem"><div class="activity-icon"><img src="x"></div>
<div class="activity-altcontent"><h4>Key info</h4><p>Labs in <span>Rankine</span> 317</p></div></li>
<li class="activity"><a href="https://moodle.example/mod/url/view.php?id=9"><span>Video 6</span><span class="accesshide sr-only"> URL</span></a></li>
</ul></div></body></html>"""


class V2Tests(unittest.TestCase):
    def test_course_code(self):
        self.assertEqual(sync.course_code({"fullname": "ENG3999 - Example Design Project 26/27"}), "ENG3999")
        self.assertIsNone(sync.course_code({"fullname": "Good Academic Practice"}))

    def test_activity_dates_are_uk_local(self):
        d = sync.activity_dates(ASSIGN_HTML)
        self.assertEqual(d["Due"], "2026-10-30T17:00:00+00:00")  # GMT after clocks change
        self.assertTrue(d["Opened"].startswith("2026-03-21T00:00"))

    def test_page_md_strips_junk_and_keeps_content(self):
        md = render.page_md(SECTION_HTML)
        self.assertIn("Key info", md)
        self.assertIn("Rankine 317", md.replace("\n", " "))
        self.assertIn("Video 6", md)
        self.assertNotIn("Section outline", md)
        self.assertNotIn("hidden", render.page_md(ASSIGN_HTML))

    def test_pluginfile_links(self):
        cs = sync.CourseSync.__new__(sync.CourseSync)
        links = cs.pluginfile_links(ASSIGN_HTML, "/mod_assign/introattachment/")
        self.assertEqual(len(links), 1)
        self.assertEqual(sync.filename_from_url(links[0][0]), "Brief 2026.pdf")

    def test_strip_volatile(self):
        self.assertNotIn("remaining", sync.strip_volatile("Lab 2: 9 days 5 hours remaining"))

    def test_todo_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["MOODLECTL_CORPUS"] = tmp
            soon = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
            past = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
            sync.write_json(Path(tmp) / "_moodle" / "courses.json", {"ENG1": {"id": 1, "name": "ENG1 x"}})
            items = {
                "1": {"module": "assign", "name": "Report", "dates": {"Due": soon}, "status": {"Submission status": "No submissions have been made yet"}},
                "2": {"module": "assign", "name": "Onramp", "dates": {"Due": past}, "status": {"Submission status": "No submissions have been made yet"}},
                "3": {"module": "assign", "name": "Lab 1", "dates": {"Due": soon}, "status": {"Submission status": "Submitted for grading"}},
                "4": {"module": "assign", "name": "Pitch (dummy submission link)", "dates": {"Due": soon}, "status": {}},
            }
            sync.write_json(Path(tmp) / "ENG1" / "moodle" / "index.json", {"items": items})
            flags = {r["name"]: r["flag"] for r in cli.collect_todo(14, False)}
            self.assertEqual(flags, {"Report": "todo", "Onramp": "OVERDUE", "Lab 1": "done", "Pitch (dummy submission link)": "no-submission"})
            del os.environ["MOODLECTL_CORPUS"]

    def test_same_named_files_get_distinct_paths(self):
        class FakeMoodle:
            def download(self, url, dest, max_bytes=None):
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(url.encode())
                return {"size": len(url)}
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["MOODLECTL_CORPUS"] = tmp
            cs = sync.CourseSync(FakeMoodle(), "ENG1", 1, "x", log=lambda s: None)
            a = cs.fetch_file("u1", cs.file_dest(None, "Revision.pdf"), "a", {})
            b = cs.fetch_file("u2", cs.file_dest(None, "Revision.pdf"), "b", {})
            self.assertEqual(a["path"], "files/Revision.pdf")
            self.assertEqual(b["path"], "files/Revision (2).pdf")
            del os.environ["MOODLECTL_CORPUS"]

    def test_parse_since(self):
        self.assertLess(cli.parse_since("24h"), datetime.now(timezone.utc))


if __name__ == "__main__":
    unittest.main()
