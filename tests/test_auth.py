import io
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from moodlectl import main as M  # noqa: E402
from moodlectl.auth import extract_cookie_value  # noqa: E402


class ExtractCookieTests(unittest.TestCase):
    def test_formats(self) -> None:
        cases = {
            "abc123def456": "abc123def456",
            "MoodleSession=abc123def456": "abc123def456",
            "other=1; MoodleSession=abc123def456; x=y": "abc123def456",
            "# Netscape HTTP Cookie File\n.moodle.gla.ac.uk\tTRUE\t/\tTRUE\t0\tMoodleSession\tabc123def456\n": "abc123def456",
            '[{"name": "MoodleSession", "value": "abc123def456", "domain": "moodle.gla.ac.uk"}]': "abc123def456",
            '{"cookies": [{"name": "MoodleSession", "value": "abc123def456"}]}': "abc123def456",
        }
        for raw, want in cases.items():
            self.assertEqual(extract_cookie_value(raw), want, raw)

    def test_rejects_garbage(self) -> None:
        with self.assertRaises(ValueError):
            extract_cookie_value("not a cookie!")


class AuthCommandTests(unittest.TestCase):
    def run_cli(self, argv, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(stdin)), redirect_stdout(out), redirect_stderr(err):
            rc = M.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_set_cookie_stdin_status_clear(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"MOODLECTL_CONFIG_DIR": tmp}, clear=False):
            os.environ.pop("MOODLE_SESSION", None)
            os.environ.pop("MOODLE_BASE_URL", None)
            with mock.patch.object(M, "Moodle") as fake:
                rc, _, err = self.run_cli(["auth", "set-cookie", "--stdin"], stdin="MoodleSession=abc123def456\n")
                self.assertEqual(rc, 0, err)
                fake.return_value.dashboard.assert_called_once()
            cfg_file = Path(tmp) / "config.json"
            cfg = json.loads(cfg_file.read_text())
            self.assertEqual(cfg["moodle_session"], "abc123def456")
            self.assertEqual(cfg["base_url"], "https://moodle.gla.ac.uk")
            self.assertEqual(stat.S_IMODE(cfg_file.stat().st_mode), 0o600)

            rc, out, _ = self.run_cli(["--json", "auth", "status", "--offline"])
            self.assertEqual(rc, 0)
            self.assertNotIn("abc123def456", out)
            self.assertEqual(json.loads(out)["cookie_source"], "config")

            rc, _, _ = self.run_cli(["auth", "clear"])
            self.assertEqual(rc, 0)
            rc, _, _ = self.run_cli(["auth", "status", "--offline"])
            self.assertEqual(rc, 2)

    def test_expired_cookie_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"MOODLECTL_CONFIG_DIR": tmp, "MOODLE_SESSION": "abc123def456"}):
            with mock.patch.object(M, "Moodle") as fake:
                fake.return_value.dashboard.side_effect = M.AuthExpired("redirected to login")
                rc, out, _ = self.run_cli(["auth", "status"])
            self.assertEqual(rc, 2)
            self.assertIn("expired", out)


class FirstRunTests(unittest.TestCase):
    run_cli = AuthCommandTests.run_cli

    def test_offline_commands_fail_before_first_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"MOODLECTL_CORPUS": tmp}):
            for argv in (["todo"], ["news"], ["grades"], ["changes"], ["find", "x"]):
                rc, out, err = self.run_cli(argv)
                self.assertEqual(rc, 1, argv)
                self.assertIn("moodlectl sync", err)


if __name__ == "__main__":
    unittest.main()
