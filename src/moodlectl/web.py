"""HTTP layer for moodlectl v2: one authenticated session, clear auth failures, polite downloads."""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import requests

DEFAULT_BASE_URL = "https://moodle.gla.ac.uk"


def config_dir() -> Path:
    env = os.environ.get("MOODLECTL_CONFIG_DIR")
    return Path(env) if env else Path.home() / ".config" / "moodlectl"


def config_path() -> Path:
    return config_dir() / "config.json"


def load_config() -> dict[str, Any]:
    p = config_path()
    return json.loads(p.read_text()) if p.exists() else {}


def save_config(data: dict[str, Any]) -> None:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2) + "\n")
    os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)


class MoodleError(Exception):
    exit_code = 1


class AuthExpired(MoodleError):
    exit_code = 2

    def __init__(self, detail: str = "") -> None:
        hint = "Moodle session has expired or was rejected. " + os.environ.get(
            "MOODLECTL_REAUTH_HINT", "Refresh the session cookie (`moodlectl auth set-cookie --stdin` or `moodlectl auth import-cookie-file`)."
        )
        super().__init__(f"{hint}{' (' + detail + ')' if detail else ''}")


class OffSite(MoodleError):
    """A Moodle link redirected to an external site (not an auth problem)."""


SSO_HOSTS = ("login.microsoftonline.com", "login.microsoft.com", "milngavie.cent.gla.ac.uk")


def is_login_url(p, moodle_host: str) -> bool:
    if p.netloc == moodle_host:
        return p.path.startswith("/login/") or p.path.startswith("/auth/")
    return p.netloc in SSO_HOSTS or "/idp/profile/" in p.path


SESSKEY_PATTERNS = [
    r'"sesskey"\s*:\s*"([^"]+)"',
    r'name="sesskey"\s+value="([^"]+)"',
    r'value="([^"]+)"\s+name="sesskey"',
]


class Moodle:
    def __init__(self, base_url: str | None = None, cookie: str | None = None, timeout: int = 60) -> None:
        cfg = load_config()
        self.base_url = (base_url or os.environ.get("MOODLE_BASE_URL") or cfg.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        cookie = cookie or os.environ.get("MOODLE_SESSION") or cfg.get("moodle_session")
        if not cookie:
            raise AuthExpired("no MoodleSession cookie configured")
        self.host = urlparse(self.base_url).netloc
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "moodlectl/2 (+https://github.com/chapmanwm/moodlectl)"
        self.s.cookies.set("MoodleSession", cookie, domain=self.host)
        self._sesskey: str | None = None
        self._userid: int | None = None

    # -- basics ---------------------------------------------------------
    def url(self, path_or_url: str) -> str:
        return path_or_url if path_or_url.startswith("http") else urljoin(self.base_url + "/", path_or_url.lstrip("/"))

    def _check(self, r: requests.Response) -> requests.Response:
        final = urlparse(r.url)
        if is_login_url(final, self.host):
            raise AuthExpired(f"redirected to {final.netloc}{final.path}")
        if final.netloc != self.host and "pluginfile" not in r.url:
            raise OffSite(f"link leaves Moodle: {r.url}")
        if r.status_code in (401, 403) and "pluginfile" not in r.url:
            raise AuthExpired(f"HTTP {r.status_code}")
        r.raise_for_status()
        return r

    def get(self, path_or_url: str, **kw: Any) -> requests.Response:
        kw.setdefault("timeout", self.timeout)
        return self._check(self.s.get(self.url(path_or_url), **kw))

    def html(self, path_or_url: str) -> str:
        return self.get(path_or_url).text

    def head_location(self, path_or_url: str) -> str | None:
        """For mod/resource and mod/url: the redirect target without downloading it."""
        r = self.s.get(self.url(path_or_url), allow_redirects=False, timeout=self.timeout)
        loc = r.headers.get("location")
        if loc:
            loc = urljoin(self.base_url, loc)
            if is_login_url(urlparse(loc), self.host):
                raise AuthExpired("redirected to login")
            return loc
        if r.status_code >= 400:
            r.raise_for_status()
        self._check(r)
        return None

    # -- session --------------------------------------------------------
    def dashboard(self) -> str:
        h = self.html("/my/")
        for pat in SESSKEY_PATTERNS:
            m = re.search(pat, h)
            if m:
                self._sesskey = m.group(1)
                break
        m = re.search(r'"userId"\s*:\s*(\d+)', h) or re.search(r'data-userid="(\d+)"', h)
        if m:
            self._userid = int(m.group(1))
        if not self._sesskey:
            raise AuthExpired("dashboard loaded without sesskey")
        return h

    @property
    def sesskey(self) -> str:
        if not self._sesskey:
            self.dashboard()
        return self._sesskey  # type: ignore[return-value]

    @property
    def userid(self) -> int | None:
        if self._userid is None:
            self.dashboard()
        return self._userid

    def ajax(self, method: str, args: dict[str, Any]) -> Any:
        r = self.s.post(
            f"{self.base_url}/lib/ajax/service.php?sesskey={self.sesskey}&info={method}",
            json=[{"index": 0, "methodname": method, "args": args}],
            timeout=self.timeout,
        )
        self._check(r)
        data = r.json()
        if isinstance(data, dict) and data.get("error"):
            if "sesskey" in str(data) or data.get("errorcode") in ("servicerequireslogin", "invalidsesskey"):
                raise AuthExpired(data.get("error", ""))
            raise MoodleError(str(data.get("error")))
        item = data[0]
        if item.get("error"):
            exc = item.get("exception") or {}
            if exc.get("errorcode") in ("servicerequireslogin", "requireloginerror", "invalidsesskey"):
                raise AuthExpired(exc.get("message", ""))
            raise MoodleError(exc.get("message") or f"{method} failed")
        return item["data"]

    # -- AJAX wrappers ---------------------------------------------------
    def courses(self, classification: str = "inprogress") -> list[dict[str, Any]]:
        out, offset = [], 0
        while True:
            d = self.ajax(
                "core_course_get_enrolled_courses_by_timeline_classification",
                {"offset": offset, "limit": 50, "classification": classification, "sort": "fullname"},
            )
            out += d.get("courses", [])
            if len(d.get("courses", [])) < 50:
                return out
            offset += 50

    def course_state(self, course_id: int) -> dict[str, Any]:
        raw = self.ajax("core_courseformat_get_state", {"courseid": course_id})
        return json.loads(raw) if isinstance(raw, str) else raw

    def course_events(self, course_id: int) -> list[dict[str, Any]]:
        try:
            return self.ajax("core_calendar_get_action_events_by_course", {"courseid": course_id, "limitnum": 50}).get("events", [])
        except MoodleError:
            return []

    # -- downloads ------------------------------------------------------
    def download(self, url: str, dest: Path, max_bytes: int | None = None) -> dict[str, Any]:
        with self.s.get(self.url(url), stream=True, timeout=max(self.timeout, 300)) as r:
            self._check(r)
            size = int(r.headers.get("content-length") or 0)
            if max_bytes and size > max_bytes:
                return {"skipped": f"too large ({size // 1_000_000} MB)", "size": size}
            if "text/html" in (r.headers.get("content-type") or "") and "pluginfile" not in r.url:
                return {"skipped": "not a file (HTML page)"}
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".part")
            n = 0
            with open(tmp, "wb") as fh:
                for chunk in r.iter_content(1 << 16):
                    n += len(chunk)
                    if max_bytes and n > max_bytes:
                        fh.close()
                        tmp.unlink(missing_ok=True)
                        return {"skipped": f"too large (>{max_bytes // 1_000_000} MB)"}
                    fh.write(chunk)
            tmp.replace(dest)
            return {"size": n, "content_type": r.headers.get("content-type")}


def filename_from_url(url: str) -> str:
    return unquote(Path(urlparse(url).path).name)
