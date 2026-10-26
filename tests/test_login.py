import io
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from moodlectl import login as L  # noqa: E402
from moodlectl import main as M  # noqa: E402
from moodlectl import timetable as T  # noqa: E402

BASE = "https://moodle.gla.ac.uk"
MS = "https://login.microsoftonline.com/common/oauth2/authorize"


def P(url=MS, body="", **kw):
    return L.Probe(url=url, body=body, **kw)


class ClassifyTests(unittest.TestCase):
    def test_states_in_login_order(self) -> None:
        cases = [
            (P(BASE + "/my/", "Dashboard"), "done"),
            (P(BASE + "/my/courses.php", "My courses"), "done"),
            (P(BASE + "/login/index.php", "Log in"), "moodle_login_page"),
            (P("https://milngavie.cent.gla.ac.uk/idp/profile/SAML2", "Select your login", has_uofg_choice=True), "choose_uofg"),
            (P(body="Sign in\nEmail, phone, or Skype", has_email_input=True, has_password_input=True), "email"),
            (P(body="x@student.gla.ac.uk\nEnter password", has_email_input=True, has_password_input=True), "password"),
            (P(body="Stay signed in?\nDo this to reduce the number of times you are asked to sign in."), "stay_signed_in"),
            (P(body="Pick an account", has_account_tile=True), "pick_account"),
            (P(body="Verify your identity\nApprove a request on my Microsoft Authenticator app", has_approve_option=True), "choose_push"),
            (P(body="Approve sign in request\nEnter the number shown", mfa_display="47"), "mfa_number:47"),
            (P(body="Approve sign in request"), "mfa_waiting"),
            (P(body="Request denied\nWe didn't hear from you"), "mfa_failed"),
            (P(body="Request wasn't sent"), "mfa_failed"),
            (P(body="Enter password\nYour account or password is incorrect.", has_password_input=True), "bad_credentials"),
            (P(body="This username may be incorrect.", has_email_input=True), "bad_credentials"),
            (P(body="Loading…"), "wait"),
        ]
        for probe, want in cases:
            self.assertEqual(L.classify(probe, BASE), want, probe.body or probe.url)

    def test_password_step_never_refills_email(self) -> None:
        # both inputs exist in the DOM on Microsoft's page; the password text must win
        probe = P(body="Enter password", has_email_input=True, has_password_input=True)
        self.assertEqual(L.classify(probe, BASE), "password")


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"MOODLECTL_CONFIG_DIR": self.tmp.name}, clear=False)
        self.env.start()
        for k in ("MOODLE_USERNAME", "MOODLE_PASSWORD", "UOFG_GUID", "MOODLE_SESSION", "MOODLECTL_SECRETS_DIR", "MOODLECTL_AUTH_STATUS_FILE"):
            os.environ.pop(k, None)

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def test_credentials_saved_private_and_env_overrides(self) -> None:
        p = L.save_credentials("1234567x@student.gla.ac.uk", "hunter2")
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(p.parent.stat().st_mode), 0o700)
        c = L.load_credentials()
        self.assertEqual((c["email"], c["password"], c["guid"]), ("1234567x@student.gla.ac.uk", "hunter2", "1234567x"))
        with mock.patch.dict(os.environ, {"MOODLE_PASSWORD": "from-env"}):
            self.assertEqual(L.load_credentials()["password"], "from-env")

    def test_separate_secrets_dir(self) -> None:
        with tempfile.TemporaryDirectory() as sec, mock.patch.dict(os.environ, {"MOODLECTL_SECRETS_DIR": sec}):
            self.assertEqual(L.save_credentials("a@b.c", "pw").parent, Path(sec))
            self.assertEqual(L.profile_dir(), Path(sec) / "sso-profile")
            self.assertFalse((Path(self.tmp.name) / "credentials.json").exists())

    def test_cooldown(self) -> None:
        now = datetime.now(timezone.utc)
        self.assertEqual(L.cooldown_remaining({}), 0)
        self.assertGreater(L.cooldown_remaining({"mfa_failed_at": (now - timedelta(minutes=2)).isoformat()}, now), 400)
        self.assertEqual(L.cooldown_remaining({"mfa_failed_at": (now - timedelta(minutes=11)).isoformat()}, now), 0)

    def test_lock_refuses_second_login(self) -> None:
        with L.login_lock():
            with self.assertRaises(L.LoginError) as cm:
                with L.login_lock():
                    pass
            self.assertEqual(cm.exception.code, L.EXIT_BUSY)

    def test_login_saves_cookie_and_status_without_secrets(self) -> None:
        L.save_credentials("1234567x@student.gla.ac.uk", "hunter2")
        with mock.patch.object(L, "browser_login", return_value="fresh-cookie-value") as bl, \
                mock.patch.object(L, "cookie_is_live", return_value=True):
            self.assertEqual(L.login(), 0)
            bl.assert_called_once()
        cfg = json.loads((Path(self.tmp.name) / "config.json").read_text())
        self.assertEqual(cfg["moodle_session"], "fresh-cookie-value")
        status = (Path(self.tmp.name) / "login-status.json").read_text()
        self.assertNotIn("hunter2", status)
        self.assertNotIn("fresh-cookie-value", status)
        self.assertIsNone(json.loads(status)["needs_human"])

    def test_if_needed_skips_browser_when_live(self) -> None:
        with mock.patch.object(L, "cookie_is_live", return_value=True), mock.patch.object(L, "browser_login") as bl:
            self.assertEqual(L.login(if_needed=True), 0)
            bl.assert_not_called()

    def test_cooldown_blocks_without_force(self) -> None:
        L.write_status(mfa_failed_at=datetime.now(timezone.utc).isoformat())
        with mock.patch.object(L, "browser_login") as bl:
            with self.assertRaises(L.LoginError) as cm:
                L.login()
            self.assertEqual(cm.exception.code, L.EXIT_BUSY)
            bl.assert_not_called()

    def test_cli_maps_login_errors_to_exit_codes(self) -> None:
        err = io.StringIO()
        with mock.patch.object(L, "browser_login", side_effect=L.LoginError("mfa denied", L.EXIT_MFA_FAILED)), \
                redirect_stderr(err), redirect_stdout(io.StringIO()):
            rc = M.main(["login"])
        self.assertEqual(rc, 4)
        self.assertIn("LOGIN_FAILED", err.getvalue())

    def test_set_credentials_password_stdin(self) -> None:
        with mock.patch("sys.stdin", io.StringIO("s3cret\n")), mock.patch.object(L.sys.stdin, "isatty", return_value=False, create=True), \
                redirect_stdout(io.StringIO()):
            rc = M.main(["auth", "set-credentials", "--email", "1234567x@student.gla.ac.uk", "--password-stdin"])
        self.assertEqual(rc, 0)
        self.assertEqual(L.load_credentials()["password"], "s3cret")
        with redirect_stdout(io.StringIO()):
            M.main(["auth", "forget-credentials"])
        self.assertEqual(L.load_credentials()["password"], "")


ICS = (
    "BEGIN:VCALENDAR\r\n"
    "BEGIN:VEVENT\r\nDTSTART:20261023T090000Z\r\nDTEND:20261023T100000Z\r\nSUMMARY:ENG1234 Lecture\\, part 1\r\n"
    "LOCATION:Example Building 101\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nDTSTART;TZID=Europe/London:20261026T140000\r\nDTEND;TZID=Europe/London:20261026T160000\r\n"
    "SUMMARY:ENG1234 Lab with a very long folded\r\n  title\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nDTSTART;VALUE=DATE:20261027\r\nSUMMARY:Reading week\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nDTSTART:20261201T090000Z\r\nSUMMARY:Far future\r\nEND:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)


class TimetableTests(unittest.TestCase):
    def test_parse_and_render_uk_local_across_clock_change(self) -> None:
        evs = T.parse_events(ICS)
        self.assertEqual(len(evs), 4)
        now = datetime(2026, 10, 23, 8, 0, tzinfo=T.UK)
        up = T.upcoming(evs, 7, now)
        self.assertEqual([e["summary"] for e in up], ["ENG1234 Lecture, part 1", "ENG1234 Lab with a very long folded title", "Reading week"])
        text = T.render(up, 7)
        self.assertIn("- 10:00–11:00 ENG1234 Lecture, part 1 — Example Building 101", text)  # 09:00Z is 10:00 BST
        self.assertIn("- 14:00–16:00 ENG1234 Lab", text)  # after the clocks go back, still local 14:00
        self.assertIn("- all day Reading week", text)
        self.assertEqual(T.to_json(up[0])["start"], "2026-10-23T10:00:00+01:00")

    def test_fetch_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "t.ics"
            resp = mock.Mock(status_code=401)
            with mock.patch.object(T.requests, "get", return_value=resp):
                with self.assertRaises(PermissionError):
                    T.fetch("guid", "pw", dest)
            resp = mock.Mock(status_code=200, text="<html>login</html>")
            with mock.patch.object(T.requests, "get", return_value=resp):
                with self.assertRaises(ValueError):
                    T.fetch("guid", "pw", dest)
            resp = mock.Mock(status_code=200, text=ICS)
            with mock.patch.object(T.requests, "get", return_value=resp):
                self.assertEqual(T.fetch("guid", "pw", dest), 4)
            self.assertTrue(dest.exists())


if __name__ == "__main__":
    unittest.main()
