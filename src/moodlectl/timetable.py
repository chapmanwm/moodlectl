"""`moodlectl timetable`: your UofG class timetable (the MyCampus/SPaG iCal feed) as UK-local text.

The feed uses HTTP basic auth with your GUID and password, so it needs saved credentials
(`moodlectl auth set-credentials`) or MOODLE_USERNAME/MOODLE_PASSWORD (+ UOFG_GUID if your GUID isn't
the part of your email before the @).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

UK = ZoneInfo("Europe/London")
TIMETABLE_URL = "https://frontdoor.spa.gla.ac.uk/spacett/download/uogtimetable.ics"


def fetch(guid: str, password: str, dest: Path, url: str = TIMETABLE_URL) -> int:
    """Download the feed atomically to dest. Returns the number of events."""
    r = requests.get(url, auth=(guid, password), timeout=60, headers={"User-Agent": "moodlectl"})
    if r.status_code in (401, 403):
        raise PermissionError("the timetable feed rejected your GUID/password")
    r.raise_for_status()
    text = r.text
    if not text.lstrip().startswith("BEGIN:VCALENDAR"):
        raise ValueError("the timetable feed did not return a calendar")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_text(text)
    tmp.replace(dest)
    return text.count("BEGIN:VEVENT")


def _unfold(text: str) -> str:
    return re.sub(r"\r?\n[ \t]", "", text)


def _parse_dt(val: str, params: str) -> tuple[datetime, bool]:
    val = val.strip()
    if re.fullmatch(r"\d{8}", val):
        return datetime.strptime(val, "%Y%m%d").replace(tzinfo=UK), True
    if val.endswith("Z"):
        return datetime.strptime(val, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc), False
    tz = re.search(r"TZID=([^;:]+)", params)
    return datetime.strptime(val, "%Y%m%dT%H%M%S").replace(tzinfo=ZoneInfo(tz.group(1)) if tz else UK), False


def parse_events(text: str) -> list[dict[str, Any]]:
    out = []
    for block in _unfold(text).split("BEGIN:VEVENT")[1:]:
        ev: dict[str, Any] = {}
        for line in block.split("END:VEVENT")[0].splitlines():
            if ":" not in line:
                continue
            key, val = line.split(":", 1)
            name = key.split(";")[0].upper()
            if name in ("DTSTART", "DTEND"):
                ev[name.lower()], allday = _parse_dt(val, key)
                ev.setdefault("all_day", allday)
            elif name in ("SUMMARY", "LOCATION", "DESCRIPTION"):
                ev[name.lower()] = val.replace("\\,", ",").replace("\\;", ";").replace("\\n", " ").strip()
        if "dtstart" in ev:
            out.append(ev)
    return out


def upcoming(events: list[dict[str, Any]], days: int, now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now(UK)
    end = now + timedelta(days=days)
    keep = [e for e in events if now - timedelta(hours=1) <= e["dtstart"].astimezone(UK) <= end]
    return sorted(keep, key=lambda e: e["dtstart"])


def to_json(e: dict[str, Any]) -> dict[str, Any]:
    s = e["dtstart"].astimezone(UK)
    f = e.get("dtend")
    return {
        "start": s.isoformat(),
        "end": f.astimezone(UK).isoformat() if f else None,
        "all_day": e.get("all_day", False),
        "summary": e.get("summary", ""),
        "location": e.get("location", ""),
    }


def render(events: list[dict[str, Any]], days: int) -> str:
    """Markdown grouped by day, times already in UK local time (no UTC/BST maths for the reader)."""
    lines = [f"# Timetable: next {days} days (UK local time)"]
    day = None
    for e in events:
        s = e["dtstart"].astimezone(UK)
        if s.date() != day:
            day = s.date()
            lines += ["", f"## {s:%a %d %b}"]
        if e.get("all_day"):
            t = "all day"
        else:
            f = e.get("dtend")
            t = f"{s:%H:%M}" + (f"–{f.astimezone(UK):%H:%M}" if f and f != e["dtstart"] else "")
        loc = f" — {e['location']}" if e.get("location") else ""
        lines.append(f"- {t} {e.get('summary', '?')}{loc}")
    if day is None:
        lines += ["", "Nothing timetabled."]
    return "\n".join(lines) + "\n"
