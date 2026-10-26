import io
import sys
import textwrap
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from moodlectl import legacy_cli as cli


class SesskeyParsingTests(unittest.TestCase):
    def test_extract_sesskey_from_dashboard_json(self) -> None:
        html = r'<script>var cfg = {"sesskey":"abc123\/xyz"};</script>'
        self.assertEqual(cli.extract_sesskey(html), "abc123/xyz")

    def test_extract_sesskey_from_hidden_input_with_value_first(self) -> None:
        html = "<form><input value='hidden456' type='hidden' name='sesskey'></form>"
        self.assertEqual(cli.extract_sesskey(html), "hidden456")


class AssignmentParsingTests(unittest.TestCase):
    def test_parse_assignment_html_handles_single_quotes_and_fallback_intro(self) -> None:
        assignment_url = "https://moodle.example/mod/assign/view.php?id=42"
        html = textwrap.dedent(
            """
            <html>
              <head><title>Essay 1</title></head>
              <body>
                <h1>Essay 1</h1>
                <div data-activityname='Essay 1'></div>
                <div class='activity-description'>Draft<br>Upload</div>
                <div class='submissionstatustable other-class'>
                  <table>
                    <tr><th>Submission status</th><td>Submitted for grading</td></tr>
                    <tr><th>Grading status</th><td>Not graded</td></tr>
                  </table>
                </div>
                <input value='Add submission' type='submit'>
                <a href='/course/view.php?id=99'>Course Name</a>
                <a href='/pluginfile.php/1/assignsubmission_file/file.pdf'>Brief PDF</a>
                <a href='/pluginfile.php/1/assignsubmission_file/file.pdf'>Brief PDF</a>
              </body>
            </html>
            """
        )

        snapshot = cli.parse_assignment_html("https://moodle.example", assignment_url, html)

        self.assertEqual(snapshot["name"], "Essay 1")
        self.assertEqual(snapshot["description"], "Draft\nUpload")
        self.assertEqual(snapshot["status"]["Submission status"], "Submitted for grading")
        self.assertEqual(snapshot["primary_action"], "Add submission")
        self.assertEqual(snapshot["course"]["id"], 99)
        self.assertEqual(snapshot["course"]["viewurl"], "https://moodle.example/course/view.php?id=99")
        self.assertEqual(snapshot["links"], [{"text": "Brief PDF", "url": "https://moodle.example/pluginfile.php/1/assignsubmission_file/file.pdf"}])

    def test_parse_assignment_html_prefers_submission_button_over_nav_button(self) -> None:
        assignment_url = "https://moodle.example/mod/assign/view.php?id=43"
        html = textwrap.dedent(
            """
            <html>
              <head><title>Essay 2</title></head>
              <body>
                <nav>
                  <button><span class='sr-only'>Side panel</span></button>
                </nav>
                <div role='main'>
                  <div class='singlebutton'>
                    <form method='get' action='https://moodle.example/mod/assign/view.php'>
                      <input type='hidden' name='id' value='43'>
                      <input type='hidden' name='action' value='editsubmission'>
                      <button type='submit' class='btn btn-primary'>Add submission</button>
                    </form>
                  </div>
                </div>
                <div class='submissionstatustable'>
                  <table><tr><th>Submission status</th><td>Nothing submitted</td></tr></table>
                </div>
              </body>
            </html>
            """
        )
        snapshot = cli.parse_assignment_html("https://moodle.example", assignment_url, html)
        self.assertEqual(snapshot["primary_action"], "Add submission")


class ICSParsingTests(unittest.TestCase):
    def test_parse_ics_content_line_preserves_params(self) -> None:
        key, params, value = cli.parse_ics_content_line("DTSTART;TZID=Europe/London:20260409T120000")
        self.assertEqual(key, "DTSTART")
        self.assertEqual(params, {"TZID": "Europe/London"})
        self.assertEqual(value, "20260409T120000")

    def test_parse_ics_text_handles_folded_lines_escaped_text_and_tzid(self) -> None:
        ics_text = textwrap.dedent(
            """
            BEGIN:VCALENDAR
            BEGIN:VEVENT
            SUMMARY:Coursework due
            DESCRIPTION:Bring draft\\nUpload to portal
             and confirm receipt
            DTSTART;TZID=Europe/London:20260409T120000
            DTEND;TZID=Europe/London:20260409T130000
            CATEGORIES:ENG1234, Assessment
            LOCATION:Room\\, 101
            END:VEVENT
            END:VCALENDAR
            """
        ).strip()

        events = cli.parse_ics_text(ics_text)
        self.assertEqual(len(events), 1)
        event = events[0]

        self.assertEqual(event["summary"], "Coursework due")
        self.assertEqual(event["description"], "Bring draft\nUpload to portaland confirm receipt")
        self.assertEqual(event["location"], "Room, 101")
        self.assertEqual(event["categories"], ["ENG1234", "Assessment"])
        self.assertEqual(event["params"]["DTSTART"], {"TZID": "Europe/London"})

        expected_start = datetime(2026, 4, 9, 12, 0, tzinfo=ZoneInfo("Europe/London")).astimezone().isoformat(timespec="minutes")
        expected_end = datetime(2026, 4, 9, 13, 0, tzinfo=ZoneInfo("Europe/London")).astimezone().isoformat(timespec="minutes")
        self.assertEqual(event["start"], expected_start)
        self.assertEqual(event["end"], expected_end)


class AuthStatusTests(unittest.TestCase):
    def test_probe_auth_status_reports_dashboard_details(self) -> None:
        class FakeClient:
            base_url = "https://moodle.example"

            def get(self, path: str):
                self.last_path = path
                return mock.Mock(
                    text="<html><head><title>Dashboard | Moodle</title></head><body><input type='hidden' name='sesskey' value='sess-123'></body></html>",
                    url="https://moodle.example/my/",
                )

        status = cli.probe_auth_status(FakeClient())
        self.assertTrue(status["ok"])
        self.assertEqual(status["dashboard_url"], "https://moodle.example/my/")
        self.assertEqual(status["dashboard_title"], "Dashboard | Moodle")
        self.assertEqual(status["sesskey"], "sess-123")
        self.assertIsNone(status["failure_kind"])

    def test_probe_auth_status_distinguishes_redirected_login(self) -> None:
        class FakeClient:
            base_url = "https://moodle.example"

            def get(self, path: str):
                self.last_path = path
                return mock.Mock(
                    text="<html><head><title>Sign in to your account</title></head><body>login</body></html>",
                    url="https://login.microsoftonline.com/example/oauth2/authorize",
                )

        status = cli.probe_auth_status(FakeClient())
        self.assertFalse(status["ok"])
        self.assertEqual(status["failure_kind"], "redirected")
        self.assertIn("redirected", status["error"])

    def test_auth_status_verify_prints_live_check(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(["auth", "status", "--verify"])
        fake_status = {
            "ok": True,
            "dashboard_url": "https://moodle.example/my/",
            "dashboard_title": "Dashboard | Moodle",
            "sesskey": "sess-123",
            "failure_kind": None,
            "error": None,
        }
        with mock.patch.object(cli, "load_config", return_value={"base_url": "https://moodle.example", "moodle_session": "cookie"}), \
             mock.patch.object(cli, "get_client", return_value=object()), \
             mock.patch.object(cli, "probe_auth_status", return_value=fake_status):
            out = io.StringIO()
            with redirect_stdout(out):
                rc = cli.cmd_auth_status(args)
        self.assertEqual(rc, 0)
        printed = out.getvalue()
        self.assertIn("Cookie available: yes", printed)
        self.assertIn("Live auth check: ok", printed)
        self.assertIn("Dashboard title: Dashboard | Moodle", printed)

    def test_auth_status_verify_prints_failure_kind(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(["auth", "status", "--verify"])
        fake_status = {
            "ok": False,
            "dashboard_url": "https://login.microsoftonline.com/example/oauth2/authorize",
            "dashboard_title": "Sign in to your account",
            "sesskey": None,
            "failure_kind": "redirected",
            "error": "Request was redirected away from the Moodle dashboard.",
        }
        with mock.patch.object(cli, "load_config", return_value={"base_url": "https://moodle.example", "moodle_session": "cookie"}), \
             mock.patch.object(cli, "get_client", return_value=object()), \
             mock.patch.object(cli, "probe_auth_status", return_value=fake_status):
            out = io.StringIO()
            with redirect_stdout(out):
                rc = cli.cmd_auth_status(args)
        self.assertEqual(rc, 0)
        printed = out.getvalue()
        self.assertIn("Live auth check: failed", printed)
        self.assertIn("Failure kind: redirected", printed)
        self.assertIn("Auth error: Request was redirected away from the Moodle dashboard.", printed)


class DueSoonSnapshotTests(unittest.TestCase):
    def test_due_soon_snapshot_records_calendar_failure_and_timeline_fallback(self) -> None:
        class FakeClient:
            def timeline(self, limit: int, timesortfrom: int):
                return {
                    "events": [
                        {
                            "id": 7,
                            "name": "Third Assessed Exercise is due",
                            "activityname": "Third Assessed Exercise",
                            "activitystr": "Assignment",
                            "modulename": "assign",
                            "eventtype": "due",
                            "normalisedeventtype": "due",
                            "normalisedeventtypetext": "Due",
                            "formattedtime": "Thursday, 9 April, 16:00",
                            "overdue": False,
                            "timesort": int(datetime(2026, 4, 9, 16, 0, tzinfo=ZoneInfo("Europe/London")).timestamp()),
                            "timestart": int(datetime(2026, 4, 9, 16, 0, tzinfo=ZoneInfo("Europe/London")).timestamp()),
                            "description": "Bring solutions",
                            "url": "https://moodle.example/mod/assign/view.php?id=42",
                            "course": {"id": 1, "fullname": "Example Course", "shortname": "ENG1235", "viewurl": "https://moodle.example/course/view.php?id=1"},
                            "action": {"displayname": "View", "url": "https://moodle.example/mod/assign/view.php?id=42"},
                        }
                    ]
                }

        fixed_now = datetime(2026, 4, 7, 12, 0, tzinfo=ZoneInfo("Europe/London"))
        with mock.patch.object(cli, "now_local", return_value=fixed_now), \
             mock.patch.object(cli, "export_calendar", side_effect=RuntimeError("calendar export blew up")):
            snapshot = cli.due_soon_snapshot(FakeClient(), days=7, include_overdue=True, limit=10)

        self.assertEqual(snapshot["source"], "timeline-fallback")
        self.assertEqual(snapshot["source_context"]["calendar_export"]["ok"], False)
        self.assertIn("calendar export blew up", snapshot["source_context"]["calendar_export"]["error"])
        self.assertEqual(snapshot["source_context"]["timeline_fallback"]["ok"], True)
        self.assertEqual(len(snapshot["events"]), 1)

    def test_cmd_due_soon_prints_source_and_fallback_error(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(["due-soon", "--days", "7"])
        fake_snapshot = {
            "fetched_at": "2026-04-07T12:00:00+01:00",
            "window_days": 7,
            "source": "timeline-fallback",
            "source_context": {
                "calendar_export": {"attempted": True, "ok": False, "error": "calendar export blew up"},
                "timeline_fallback": {"attempted": True, "ok": True, "error": None},
            },
            "events": [],
        }
        with mock.patch.object(cli, "get_client", return_value=object()), \
             mock.patch.object(cli, "due_soon_snapshot", return_value=fake_snapshot):
            out = io.StringIO()
            with redirect_stdout(out):
                rc = cli.cmd_due_soon(args)
        self.assertEqual(rc, 0)
        printed = out.getvalue()
        self.assertIn("Source: timeline-fallback", printed)
        self.assertIn("Calendar export error: calendar export blew up", printed)


class ParserHelpTests(unittest.TestCase):
    def test_build_parser_mentions_vps_auth_lookup(self) -> None:
        parser = cli.build_parser()
        self.assertIn("MOODLE_SESSION", parser.epilog)
        self.assertIn("SSH/VPS", parser.epilog)

    def test_auth_status_parser_supports_verify(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(["auth", "status", "--verify"])
        self.assertTrue(args.verify)


if __name__ == "__main__":
    unittest.main()
