"""`moodlectl login`: get a MoodleSession by driving the UofG Microsoft SSO login in a headless browser.

Needs the optional extra: pip install 'moodlectl[login]' (Camoufox, a hardened Firefox build; the
browser binary downloads once on first use).

How it works
- A persistent browser profile is kept in the secrets dir. Answering "Stay signed in?" with yes means
  later logins usually complete silently, with no password and no MFA.
- Number-matching MFA: the number is printed as `MFA_NUMBER <n>` on stdout. Enter it in Microsoft
  Authenticator.
- Exactly ONE push per run. A denied/expired/throttled request ends the run (exit 4), and the next run
  waits out a 10-minute cooldown unless --force is given. Auto-resending looks like an MFA-fatigue
  attack and gets the account throttled by Microsoft.
- Wrong username/password stops immediately (exit 5), so we never hammer the account into a lockout.

Exit codes: 0 ok · 1 setup problem · 2 timed out / cookie didn't verify · 3 MFA needed but --no-mfa
· 4 MFA denied/expired/throttled · 5 wrong username or password · 6 cooldown / another login running
"""
from __future__ import annotations

import getpass
import json
import os
import re
import stat
import sys
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

from .web import DEFAULT_BASE_URL, AuthExpired, Moodle, config_dir, load_config, save_config

EMAIL_SEL = "input[name=loginfmt]:not([aria-hidden=true])"
PW_SEL = "input[name=passwd]:not([aria-hidden=true])"
MFA_COOLDOWN_S = 600

EXIT_SETUP, EXIT_TIMEOUT, EXIT_NEEDS_MFA, EXIT_MFA_FAILED, EXIT_BAD_CREDS, EXIT_BUSY = 1, 2, 3, 4, 5, 6

BAD_CREDS_RE = re.compile(
    r"account or password is incorrect|username may be incorrect|couldn't find an account|"
    r"enter a valid email|password is incorrect|account has been locked|account is locked",
    re.I,
)
MFA_FAILED_RE = re.compile(r"Request denied|didn't hear from you|Request wasn't sent|request (has )?timed out", re.I)


class LoginError(Exception):
    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


# -- storage ------------------------------------------------------------------


def secrets_dir() -> Path:
    """Credentials + SSO browser profile. Defaults to the config dir; point MOODLECTL_SECRETS_DIR somewhere
    else if an agent sandbox can read the config dir (it then sees only the cookie, not your password)."""
    env = os.environ.get("MOODLECTL_SECRETS_DIR")
    return Path(env).expanduser() if env else config_dir()


def credentials_path() -> Path:
    return secrets_dir() / "credentials.json"


def profile_dir() -> Path:
    return secrets_dir() / "sso-profile"


def status_path() -> Path:
    return Path(os.environ.get("MOODLECTL_AUTH_STATUS_FILE") or config_dir() / "login-status.json")


def _private_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    os.chmod(p, stat.S_IRWXU)
    return p


def load_credentials() -> dict[str, str]:
    """Env vars win over the saved file. Keys: email, password, guid (timetable login; defaults to the part before @)."""
    saved: dict[str, str] = {}
    p = credentials_path()
    if p.exists():
        saved = json.loads(p.read_text())
    creds = {
        "email": os.environ.get("MOODLE_USERNAME") or saved.get("email", ""),
        "password": os.environ.get("MOODLE_PASSWORD") or saved.get("password", ""),
        "guid": os.environ.get("UOFG_GUID") or saved.get("guid", ""),
    }
    if creds["email"] and not creds["guid"]:
        creds["guid"] = creds["email"].split("@")[0]
    return creds


def save_credentials(email: str, password: str, guid: str = "") -> Path:
    _private_dir(secrets_dir())
    p = credentials_path()
    p.write_text(json.dumps({"email": email, "password": password, "guid": guid or email.split("@")[0]}))
    os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    return p


def prompt_missing(creds: dict[str, str], need_email: bool, need_password: bool) -> None:
    """Ask on the terminal for whatever the current login step needs and isn't saved."""
    if not ((need_email and not creds["email"]) or (need_password and not creds["password"])):
        return
    if not sys.stdin.isatty():
        raise LoginError(
            "UofG credentials needed but none are saved and there's no terminal to ask. "
            "Run `moodlectl auth set-credentials` once, or set MOODLE_USERNAME / MOODLE_PASSWORD.",
            EXIT_SETUP,
        )
    if need_email and not creds["email"]:
        creds["email"] = input("UofG email (e.g. 1234567x@student.gla.ac.uk): ").strip()
        creds["guid"] = creds["guid"] or creds["email"].split("@")[0]
    if need_password and not creds["password"]:
        creds["password"] = getpass.getpass("UofG password (hidden): ")


def read_status() -> dict[str, Any]:
    try:
        return json.loads(status_path().read_text())
    except Exception:
        return {}


def write_status(**kw: Any) -> None:
    """Non-secret login state for agents/other tools (`moodlectl status` shows it)."""
    cur = read_status()
    cur.update(kw)
    cur["updated_at"] = _now()
    p = status_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(cur, indent=2) + "\n")
    tmp.replace(p)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def cooldown_remaining(status: dict[str, Any], now: datetime | None = None) -> int:
    failed = status.get("mfa_failed_at")
    if not failed:
        return 0
    now = now or datetime.now(timezone.utc)
    left = MFA_COOLDOWN_S - (now - datetime.fromisoformat(failed)).total_seconds()
    return max(0, int(left))


@contextmanager
def login_lock() -> Iterator[None]:
    """Two concurrent logins = two MFA pushes. Refuse the second one."""
    try:
        import fcntl
    except ImportError:  # Windows: best effort
        yield
        return
    _private_dir(secrets_dir())
    fh = open(secrets_dir() / "login.lock", "w")
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LoginError("another `moodlectl login` is already running", EXIT_BUSY) from None
        yield
    finally:
        fh.close()


# -- page state machine ---------------------------------------------------------


@dataclass
class Probe:
    """What the login loop can see on the current page."""

    url: str
    body: str
    has_email_input: bool = False
    has_password_input: bool = False
    has_uofg_choice: bool = False
    has_account_tile: bool = False
    has_approve_option: bool = False
    mfa_display: str = ""


def classify(p: Probe, moodle_base: str) -> str:
    """Return the action for this page. Order matters: it mirrors what UofG's login actually shows."""
    u = urlparse(p.url)
    base = urlparse(moodle_base)
    on_moodle = u.netloc == base.netloc
    if on_moodle and "/login" not in u.path and u.path.startswith("/my"):
        return "done"
    if BAD_CREDS_RE.search(p.body):
        return "bad_credentials"
    if MFA_FAILED_RE.search(p.body):
        return "mfa_failed"
    if "Select your login" in p.body and p.has_uofg_choice:
        return "choose_uofg"
    if on_moodle and u.path.startswith("/login"):
        return "moodle_login_page"
    if "Enter password" in p.body and p.has_password_input:
        return "password"
    if p.has_email_input and "Enter password" not in p.body:
        return "email"
    if re.search(r"Stay signed in", p.body, re.I):
        return "stay_signed_in"
    if "Pick an account" in p.body and p.has_account_tile:
        return "pick_account"
    if "Verify your identity" in p.body and p.has_approve_option:
        return "choose_push"
    m = re.search(r"\b(\d{2,3})\b", p.mfa_display or "")
    if m:
        return "mfa_number:" + m.group(1)
    if re.search(r"Approve sign in|Verify your identity", p.body, re.I):
        return "mfa_waiting"
    return "wait"


# -- browser ------------------------------------------------------------------


def _say(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _launch(headless: bool):
    try:
        from camoufox.pkgman import camoufox_path
        from camoufox.sync_api import Camoufox
    except ImportError:
        raise LoginError("Browser login needs the optional extra: pip install 'moodlectl[login]'", EXIT_SETUP) from None
    try:
        camoufox_path(download_if_missing=False)
    except Exception:
        _say("First run: downloading the Camoufox browser (one-off, ~1.3 GB)…")
        camoufox_path(download_if_missing=True)
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        os.environ.pop("XDG_RUNTIME_DIR", None)  # Firefox refuses a runtime dir owned by another user
    _private_dir(secrets_dir())
    _private_dir(profile_dir())
    return Camoufox(
        headless=headless,
        persistent_context=True,
        user_data_dir=str(profile_dir()),
        humanize=True,
        os=["windows", "macos", "linux"],  # os=["linux"] alone can fail fingerprint generation
    )


def _probe(page, email: str) -> Probe:
    try:
        body = (page.inner_text("body") or "").strip()  # inner_text, not text_content: MS pages embed scripts
    except Exception:
        body = ""
    disp = page.locator("#idRichContext_DisplaySign")
    tile = page.locator("[data-test-id]").filter(has_text=email) if email else None
    return Probe(
        url=page.url,
        body=body,
        has_email_input=page.locator(EMAIL_SEL).count() > 0,
        has_password_input=page.locator(PW_SEL).count() > 0,
        has_uofg_choice=page.get_by_text(re.compile(r"UofG email address", re.I)).count() > 0,
        has_account_tile=bool(tile and tile.count()),
        has_approve_option=page.get_by_text(re.compile(r"Approve a request", re.I)).count() > 0,
        mfa_display=(disp.first.text_content() or "") if disp.count() else "",
    )


def browser_login(
    *,
    timeout: int = 300,
    no_mfa: bool = False,
    headless: bool = True,
    debug: bool = False,
    base_url: str | None = None,
) -> str:
    """Drive the SSO flow until Moodle's dashboard loads; return the MoodleSession cookie value."""
    base = (base_url or os.environ.get("MOODLE_BASE_URL") or load_config().get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    creds = load_credentials()
    debug_dir = config_dir() / "login-debug"
    with ExitStack() as stack:
        try:
            ctx = stack.enter_context(_launch(headless))
        except LoginError:
            raise
        except Exception as exc:
            first = (str(exc).strip().splitlines() or [type(exc).__name__])[0][:200]
            raise LoginError(
                f"Couldn't start the login browser ({first}). On Linux, install its system libraries with "
                "`python -m playwright install-deps firefox` (needs sudo/root), then retry.",
                EXIT_SETUP,
            ) from None
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(f"{base}/my/", wait_until="networkidle", timeout=90_000)
        deadline = time.time() + timeout
        last, mfa_number = None, None
        while time.time() < deadline:
            probe = _probe(page, creds["email"])
            action = classify(probe, base)
            if action != last:
                _say(f"login: {action} ({urlparse(probe.url).netloc})")
                if debug:
                    _private_dir(debug_dir)
                    page.screenshot(path=str(debug_dir / f"{int(time.time())}-{action.split(':')[0]}.png"))
                    _say("  " + " ".join(probe.body[:200].split()))
                last = action
            if action == "done":
                break
            if action == "bad_credentials":
                write_status(moodle_live=False, mfa_pending=False, needs_human="bad-credentials")
                raise LoginError(
                    "Microsoft rejected the username or password. Fix them with `moodlectl auth set-credentials` "
                    "(stopping now so the account doesn't get locked).",
                    EXIT_BAD_CREDS,
                )
            if action == "mfa_failed":
                reason = "throttled" if "Request wasn't sent" in probe.body else "denied-or-expired"
                write_status(moodle_live=False, mfa_pending=False, mfa_number=None, needs_human=f"mfa-{reason}", mfa_failed_at=_now())
                raise LoginError(
                    f"MFA request {reason.replace('-', ' ')}. Not resending automatically. "
                    f"Run `moodlectl login` again when you're ready (after a {MFA_COOLDOWN_S // 60}-minute cooldown, or with --force).",
                    EXIT_MFA_FAILED,
                )
            if action == "choose_uofg":
                page.get_by_text(re.compile(r"UofG email address", re.I)).first.click(timeout=30_000)
            elif action == "moodle_login_page":
                btn = page.get_by_role("button", name=re.compile("UofG email", re.I))
                if btn.count():
                    btn.first.click(timeout=30_000)
                else:
                    page.goto(f"{base}/my/", wait_until="networkidle", timeout=90_000)
            elif action == "password":
                if not creds["password"]:
                    prompt_missing(creds, need_email=False, need_password=True)
                page.wait_for_timeout(1000)  # slide-in animation
                pw = page.locator(PW_SEL).first
                pw.fill("")
                pw.press_sequentially(creds["password"], delay=40)  # fill()+click submitted empty passwords
                if len(pw.input_value() or "") != len(creds["password"]):
                    continue
                pw.press("Enter")  # the footer overlay intercepts clicks on #idSIButton9
                page.wait_for_timeout(4000)
            elif action == "email":
                if not creds["email"]:
                    prompt_missing(creds, need_email=True, need_password=False)
                em = page.locator(EMAIL_SEL).first
                em.fill(creds["email"])
                em.press("Enter")
                page.wait_for_timeout(3000)
            elif action == "stay_signed_in":
                cb = page.locator("#KmsiCheckboxField")
                if cb.count():
                    cb.first.check()
                page.locator("#idSIButton9").first.click(force=True)  # "Yes": makes later logins silent
            elif action == "pick_account":
                page.locator("[data-test-id]").filter(has_text=creds["email"]).first.click()
            elif action == "choose_push":
                if no_mfa:
                    write_status(moodle_live=False, mfa_pending=False, needs_human="mfa")
                    raise LoginError("MFA is required and --no-mfa was given (no push sent).", EXIT_NEEDS_MFA)
                page.get_by_text(re.compile(r"Approve a request", re.I)).first.click(force=True)
                page.wait_for_timeout(3000)
            elif action.startswith("mfa_number:") or action == "mfa_waiting":
                if no_mfa:
                    write_status(moodle_live=False, mfa_pending=False, needs_human="mfa")
                    raise LoginError("MFA is required and --no-mfa was given.", EXIT_NEEDS_MFA)
                n = action.split(":", 1)[1] if ":" in action else None
                if n and n != mfa_number:
                    mfa_number = n
                    print(f"MFA_NUMBER {n}", flush=True)
                    _say(f"→ Open Microsoft Authenticator and enter {n}. (One request only; it expires in a few minutes.)")
                    write_status(mfa_pending=True, mfa_number=n, mfa_requested_at=_now())
            page.wait_for_timeout(2500)
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass
        else:
            write_status(moodle_live=False, mfa_pending=False, mfa_number=None, needs_human="login-timeout")
            raise LoginError(f"Login timed out after {timeout}s (last step: {last}). Re-run with --debug to see screenshots.", EXIT_TIMEOUT)

        host = urlparse(base).netloc
        cookies = [c for c in ctx.cookies() if c["name"] == "MoodleSession" and host.endswith(c["domain"].lstrip("."))]
        if not cookies:
            raise LoginError("Reached the Moodle dashboard but found no MoodleSession cookie.", EXIT_TIMEOUT)
        return cookies[0]["value"]


def cookie_is_live() -> bool:
    try:
        Moodle().dashboard()
        return True
    except AuthExpired:
        return False


def login(*, if_needed: bool = False, force: bool = False, **kw: Any) -> int:
    """Full `moodlectl login` flow. Returns an exit code; never prints secrets."""
    if if_needed and cookie_is_live():
        write_status(moodle_live=True, needs_human=None)
        _say("Moodle session is already live; nothing to do.")
        return 0
    left = cooldown_remaining(read_status())
    if left and not force:
        raise LoginError(f"The last MFA attempt failed recently; waiting {left // 60 + 1} more min avoids Microsoft throttling (or pass --force).", EXIT_BUSY)
    with login_lock():
        cookie = browser_login(**kw)
    cfg = load_config()
    cfg["moodle_session"] = cookie
    cfg["base_url"] = (kw.get("base_url") or cfg.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    save_config(cfg)
    os.environ.pop("MOODLE_SESSION", None)  # verify the cookie we just saved, not a stale env one
    ok = cookie_is_live()
    write_status(moodle_live=ok, mfa_pending=False, mfa_number=None, needs_human=None if ok else "verify-failed",
                 mfa_failed_at=None, moodle_login_at=_now())
    _say("Logged in; Moodle session saved and verified." if ok else "Got a cookie but Moodle rejected it.")
    return 0 if ok else EXIT_TIMEOUT
