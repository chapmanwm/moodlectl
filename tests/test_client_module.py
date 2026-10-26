import argparse
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from moodlectl import client


class ClientModuleTests(unittest.TestCase):
    def test_get_client_uses_config_cookie_and_base_url_by_default(self) -> None:
        args = argparse.Namespace(cookie=None, base_url=None, timeout=45)
        with mock.patch.object(client, "load_config", return_value={"moodle_session": "cookie-123", "base_url": "https://moodle.example"}):
            moodle_client = client.get_client(args)
        self.assertEqual(moodle_client.base_url, "https://moodle.example")
        self.assertEqual(moodle_client.moodle_session, "cookie-123")
        self.assertEqual(moodle_client.timeout, 45)

    def test_get_client_prefers_explicit_args_over_config(self) -> None:
        args = argparse.Namespace(cookie="arg-cookie", base_url="https://override.example", timeout=30)
        with mock.patch.object(client, "load_config", return_value={"moodle_session": "config-cookie", "base_url": "https://moodle.example"}):
            moodle_client = client.get_client(args)
        self.assertEqual(moodle_client.base_url, "https://override.example")
        self.assertEqual(moodle_client.moodle_session, "arg-cookie")

    def test_moodle_client_get_rejects_login_redirect(self) -> None:
        moodle_client = client.MoodleClient(base_url="https://moodle.example", moodle_session="cookie")
        fake_response = mock.Mock(url="https://moodle.example/login/index.php", status_code=200)
        fake_response.raise_for_status.return_value = None
        with mock.patch.object(moodle_client.session, "get", return_value=fake_response):
            with self.assertRaises(client.MoodleCtlError):
                moodle_client.get("/my/")


if __name__ == "__main__":
    unittest.main()
