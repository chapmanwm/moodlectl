from __future__ import annotations

import argparse
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from .parsers import extract_sesskey

DEFAULT_BASE_URL = "https://moodle.gla.ac.uk"
CONFIG_DIR = Path(os.environ["MOODLECTL_CONFIG_DIR"]) if os.environ.get("MOODLECTL_CONFIG_DIR") else Path.home() / ".config" / "moodlectl"
CONFIG_PATH = CONFIG_DIR / "config.json"


class MoodleCtlError(Exception):
    pass


@dataclass
class MoodleClient:
    base_url: str
    moodle_session: str
    timeout: int = 30

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "moodlectl/0.2"})
        self.session.cookies.set("MoodleSession", self.moodle_session, domain=self._cookie_domain())
        self._sesskey: str | None = None

    def _cookie_domain(self) -> str:
        return re.sub(r"^https?://", "", self.base_url).split("/")[0]

    def get(self, path_or_url: str, *, allow_redirects: bool = True) -> requests.Response:
        url = path_or_url if path_or_url.startswith("http") else f"{self.base_url}/{path_or_url.lstrip('/')}"
        response = self.session.get(url, timeout=self.timeout, allow_redirects=allow_redirects)
        response.raise_for_status()
        if "login/index.php" in response.url:
            raise MoodleCtlError("Cookie was rejected; Moodle redirected to the login page.")
        return response

    def dashboard_html(self) -> str:
        return self.get("/my/").text

    def sesskey(self) -> str:
        if self._sesskey is None:
            sesskey = extract_sesskey(self.dashboard_html())
            if not sesskey:
                raise MoodleCtlError("Could not extract sesskey from dashboard HTML.")
            self._sesskey = sesskey
        return self._sesskey

    def ajax(self, method: str, args: dict[str, Any]) -> Any:
        url = f"{self.base_url}/lib/ajax/service.php?sesskey={self.sesskey()}&info={method}"
        payload = [{"index": 0, "methodname": method, "args": args}]
        response = self.session.post(url, json=payload, timeout=self.timeout)
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict) and data.get("error"):
            raise MoodleCtlError(data.get("error", "Unknown Moodle AJAX error"))
        if not isinstance(data, list) or not data:
            raise MoodleCtlError(f"Unexpected AJAX response shape for {method!r}: {data!r}")
        result = data[0]
        if result.get("error"):
            exc = result.get("exception") or {}
            raise MoodleCtlError(exc.get("message") or f"Moodle returned an error for {method}")
        return result["data"]

    def courses(
        self,
        classification: str = "inprogress",
        limit: int = 50,
        offset: int = 0,
        sort: str = "fullname",
    ) -> dict[str, Any]:
        return self.ajax(
            "core_course_get_enrolled_courses_by_timeline_classification",
            {
                "offset": offset,
                "limit": limit,
                "classification": classification,
                "sort": sort,
            },
        )

    def timeline(self, limit: int = 25, timesortfrom: int = 0, courseid: int | None = None) -> dict[str, Any]:
        limit = max(1, min(limit, 50))
        args: dict[str, Any] = {
            "limitnum": limit,
            "timesortfrom": timesortfrom,
        }
        if courseid is not None:
            args["courseid"] = courseid
        return self.ajax("core_calendar_get_action_events_by_timesort", args)

    def course_timeline(self, courseid: int, limit: int = 50) -> dict[str, Any]:
        limit = max(1, min(limit, 50))
        return self.ajax("core_calendar_get_action_events_by_course", {"courseid": courseid, "limitnum": limit})

    def course_state(self, courseid: int) -> dict[str, Any]:
        raw = self.ajax("core_courseformat_get_state", {"courseid": courseid})
        if isinstance(raw, str):
            return json.loads(raw)
        return raw


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    return json.loads(CONFIG_PATH.read_text())



def save_config(data: dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(data, indent=2) + "\n")
    os.chmod(CONFIG_PATH, stat.S_IRUSR | stat.S_IWUSR)



def get_client(args: argparse.Namespace) -> MoodleClient:
    config = load_config()
    moodle_session = args.cookie or os.environ.get("MOODLE_SESSION") or config.get("moodle_session")
    if not moodle_session:
        raise MoodleCtlError(
            "No MoodleSession cookie found. Use 'moodlectl auth set-cookie <cookie>' or set MOODLE_SESSION."
        )
    base_url = args.base_url or os.environ.get("MOODLE_BASE_URL") or config.get("base_url") or DEFAULT_BASE_URL
    return MoodleClient(base_url=base_url, moodle_session=moodle_session, timeout=args.timeout)


__all__ = [
    "CONFIG_DIR",
    "CONFIG_PATH",
    "DEFAULT_BASE_URL",
    "MoodleClient",
    "MoodleCtlError",
    "get_client",
    "load_config",
    "save_config",
]
