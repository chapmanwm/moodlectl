import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from moodlectl import legacy_cli as cli

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class RealFixtureParsingTests(unittest.TestCase):
    def test_dashboard_fixture_preserves_supported_sesskey_shape(self) -> None:
        html_text = (FIXTURES / "uofg_dashboard.html").read_text()
        self.assertEqual(cli.extract_sesskey(html_text), "fixture-sesskey")

    def test_assignment_fixture_parses_expected_submission_details(self) -> None:
        html_text = (FIXTURES / "uofg_assignment.html").read_text()
        snapshot = cli.parse_assignment_html(
            "https://moodle.example",
            "https://moodle.example/mod/assign/view.php?id=999999",
            html_text,
        )
        self.assertEqual(snapshot["name"], "Third Assessed Exercise -- Submission")
        self.assertEqual(snapshot["primary_action"], "Add submission")
        self.assertEqual(snapshot["course"]["id"], 999999)
        self.assertEqual(snapshot["course"]["fullname"], "ENG1234: Example Course (2025-26)")
        self.assertEqual(snapshot["status"]["Submission status"], "No submissions have been made yet")
        self.assertIn("second assessed exercise", snapshot["description"])
        self.assertEqual(len(snapshot["links"]), 1)

    def test_calendar_fixture_parses_due_events(self) -> None:
        ics_text = (FIXTURES / "uofg_calendar.ics").read_text()
        events = cli.parse_ics_text(ics_text)
        self.assertEqual(len(events), 3)
        self.assertEqual(events[0]["summary"], "Third Assessed Exercise -- Submission is due")
        self.assertEqual(events[0]["categories"], ["ENG1234_2025"])
        self.assertIn("second assessed exercise", events[0]["description"])


if __name__ == "__main__":
    unittest.main()
