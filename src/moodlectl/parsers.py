from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def clean_html_text(value: str | None) -> str | None:
    if value is None:
        return None
    value = html.unescape(value)
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    value = re.sub(r"</p>|</div>|</li>|</tr>|</h\d>", "\n", value, flags=re.I)
    value = re.sub(r"<li[^>]*>", "- ", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    value = value.replace("\xa0", " ")
    value = re.sub(r"[ \t\r\f\v]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def decode_web_text(value: str | None) -> str | None:
    if value is None:
        return None
    value = html.unescape(value)
    value = value.replace("\\/", "/")
    return value


def find_html_match(html_text: str, patterns: list[str], *, flags: int = 0) -> re.Match[str] | None:
    for pattern in patterns:
        match = re.search(pattern, html_text, flags)
        if match:
            return match
    return None


def extract_sesskey(html_text: str) -> str | None:
    match = find_html_match(
        html_text,
        [
            r'["\']sesskey["\']\s*:\s*["\']([^"\']+)["\']',
            r'<input[^>]+name=["\']sesskey["\'][^>]+value=["\']([^"\']+)["\']',
            r'<input[^>]+value=["\']([^"\']+)["\'][^>]+name=["\']sesskey["\']',
            r'\bsesskey\b\s*=\s*["\']([^"\']+)["\']',
        ],
        flags=re.I,
    )
    return decode_web_text(match.group(1)) if match else None


def extract_assignment_intro(html_text: str) -> str | None:
    match = find_html_match(
        html_text,
        [
            r'<div[^>]*data-ally-richcontent=["\']assign:assign:intro:[^"\']*["\'][^>]*>(.*?)</div>',
            r'<div[^>]*id=["\']intro["\'][^>]*>(.*?)</div>',
            r'<div[^>]*class=["\'][^"\']*\bactivity-description\b[^"\']*["\'][^>]*>(.*?)</div>',
        ],
        flags=re.I | re.S,
    )
    return clean_html_text(match.group(1)) if match else None


def unescape_ics_text(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.replace(r"\N", "\n").replace(r"\n", "\n")
    value = value.replace(r"\,", ",").replace(r"\;", ";").replace(r"\\", "\\")
    return html.unescape(value)


def parse_ics_categories(value: str | None) -> list[str]:
    text = unescape_ics_text(value)
    if not text:
        return []
    return [part.strip() for part in text.split(",") if part.strip()]


def maybe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def extract_id_from_url(url: str, key: str = "id") -> int | None:
    try:
        parsed = urlparse(url)
        value = parse_qs(parsed.query).get(key, [None])[0]
        return maybe_int(value)
    except Exception:
        return None


def parse_assignment_html(base_url: str, assignment_url: str, html_text: str) -> dict[str, Any]:
    title_match = re.search(r"<title>(.*?)</title>", html_text, re.I | re.S)
    page_title = clean_html_text(title_match.group(1)) if title_match else None

    heading_match = find_html_match(html_text, [r"<h1[^>]*>(.*?)</h1>", r"<h2[^>]*>(.*?)</h2>"], flags=re.I | re.S)
    heading = clean_html_text(heading_match.group(1)) if heading_match else None

    activity_name_match = find_html_match(
        html_text,
        [
            r'data-region=["\']activity-information["\'][^>]*data-activityname=["\']([^"\']+)["\']',
            r'data-activityname=["\']([^"\']+)["\']',
        ],
        flags=re.I,
    )
    activity_name = decode_web_text(activity_name_match.group(1)) if activity_name_match else None

    description = extract_assignment_intro(html_text)

    status_rows: dict[str, str] = {}
    status_block_match = re.search(
        r'<div[^>]*class=["\'][^"\']*\bsubmissionstatustable\b[^"\']*["\'][^>]*>.*?<table[^>]*>(.*?)</table>',
        html_text,
        re.I | re.S,
    )
    if status_block_match:
        tbody = status_block_match.group(1)
        for row_match in re.finditer(r"<tr[^>]*>(.*?)</tr>", tbody, re.I | re.S):
            row = row_match.group(1)
            th_match = re.search(r"<th[^>]*>(.*?)</th>", row, re.I | re.S)
            td_match = re.search(r"<td[^>]*>(.*?)</td>", row, re.I | re.S)
            if not th_match or not td_match:
                continue
            key = clean_html_text(th_match.group(1))
            value = clean_html_text(td_match.group(1))
            if key:
                status_rows[key] = value or ""

    primary_action = None
    action_form_match = re.search(
        r'<form[^>]*>(?:(?!</form>).)*?<input[^>]+name=["\']action["\'][^>]+value=["\']editsubmission["\'][^>]*>(?:(?!</form>).)*?(?:<button[^>]*>(.*?)</button>|<input[^>]+type=["\']submit["\'][^>]+value=["\']([^"\']+)["\'])',
        html_text,
        re.I | re.S,
    )
    if action_form_match:
        primary_action = clean_html_text(action_form_match.group(1) or action_form_match.group(2))
    if not primary_action:
        button_match = find_html_match(
            html_text,
            [
                r'<div[^>]*class=["\'][^"\']*\bsinglebutton\b[^"\']*["\'][^>]*>.*?(?:<button[^>]*>(.*?)</button>|<input[^>]+type=["\']submit["\'][^>]+value=["\']([^"\']+)["\'])',
                r'<button[^>]*>(.*?)</button>',
                r'<input[^>]+type=["\']submit["\'][^>]+value=["\']([^"\']+)["\']',
                r'<input[^>]+value=["\']([^"\']+)["\'][^>]+type=["\']submit["\']',
            ],
            flags=re.I | re.S,
        )
        if button_match:
            primary_action = clean_html_text(next((group for group in button_match.groups() if group), None))

    links = []
    for match in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html_text, re.I | re.S):
        href = decode_web_text(match.group(1))
        text = clean_html_text(match.group(2))
        if not href or not text:
            continue
        if any(token in href for token in ["pluginfile.php", "submission", "portfolio", "download", "edit", "comment"]):
            links.append({"text": text, "url": urljoin(base_url, href)})
    unique_links = []
    seen_urls = set()
    for link in links:
        if link["url"] in seen_urls:
            continue
        seen_urls.add(link["url"])
        unique_links.append(link)

    course_match = re.search(r'<a[^>]+href=["\']([^"\']*/course/view\.php\?id=\d+[^"\']*)["\'][^>]*>(.*?)</a>', html_text, re.I | re.S)
    course = None
    if course_match:
        course_href = decode_web_text(course_match.group(1)) or ""
        course = {
            "id": extract_id_from_url(course_href),
            "fullname": clean_html_text(course_match.group(2)),
            "viewurl": urljoin(base_url, course_href),
        }

    return {
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "url": assignment_url,
        "id": extract_id_from_url(assignment_url),
        "page_title": page_title,
        "heading": heading,
        "name": activity_name or heading or page_title,
        "course": course,
        "description": description,
        "status": status_rows,
        "primary_action": primary_action,
        "links": unique_links,
    }


def parse_ics_dt(value: str | None, params: dict[str, str] | None = None) -> str | None:
    if not value:
        return None
    value = value.strip()
    params = params or {}
    try:
        if value.endswith("Z"):
            dt = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            return dt.astimezone().isoformat(timespec="minutes")
        if "T" in value:
            dt = datetime.strptime(value, "%Y%m%dT%H%M%S")
            tzid = params.get("TZID")
            if tzid:
                try:
                    dt = dt.replace(tzinfo=ZoneInfo(tzid))
                except ZoneInfoNotFoundError:
                    pass
            return dt.astimezone().isoformat(timespec="minutes")
        dt = datetime.strptime(value, "%Y%m%d")
        return dt.astimezone().isoformat(timespec="minutes")
    except ValueError:
        return value


def parse_ics_content_line(line: str) -> tuple[str, dict[str, str], str]:
    raw_key, value = line.split(":", 1)
    key_parts = raw_key.split(";")
    key = key_parts[0]
    params: dict[str, str] = {}
    for part in key_parts[1:]:
        if "=" not in part:
            continue
        param_key, param_value = part.split("=", 1)
        params[param_key.upper()] = param_value
    return key, params, value


def parse_ics_text(ics_text: str) -> list[dict[str, Any]]:
    events = []
    current: dict[str, str] | None = None
    current_params: dict[str, dict[str, str]] | None = None
    buffer_key: str | None = None
    for raw_line in ics_text.splitlines():
        if raw_line.startswith((" ", "\t")) and current is not None and buffer_key is not None:
            current[buffer_key] = current.get(buffer_key, "") + raw_line[1:]
            continue
        line = raw_line.strip()
        if line == "BEGIN:VEVENT":
            current = {}
            current_params = {}
            buffer_key = None
            continue
        if line == "END:VEVENT":
            if current is not None:
                events.append({"values": current, "params": current_params or {}})
            current = None
            current_params = None
            buffer_key = None
            continue
        if current is None or ":" not in line:
            continue
        key, params, value = parse_ics_content_line(line)
        current[key] = value
        if params and current_params is not None:
            current_params[key] = params
        buffer_key = key
    normalised = []
    for event_bundle in events:
        event = event_bundle["values"]
        event_params = event_bundle["params"]
        normalised.append(
            {
                "summary": unescape_ics_text(event.get("SUMMARY")) or "",
                "description": clean_html_text(unescape_ics_text(event.get("DESCRIPTION"))),
                "location": unescape_ics_text(event.get("LOCATION")) or "",
                "dtstart": event.get("DTSTART"),
                "dtend": event.get("DTEND"),
                "start": parse_ics_dt(event.get("DTSTART"), event_params.get("DTSTART")),
                "end": parse_ics_dt(event.get("DTEND"), event_params.get("DTEND")),
                "categories": parse_ics_categories(event.get("CATEGORIES")),
                "raw": event,
                "params": event_params,
            }
        )
    return normalised


__all__ = [
    "clean_html_text",
    "decode_web_text",
    "extract_assignment_intro",
    "extract_id_from_url",
    "extract_sesskey",
    "find_html_match",
    "maybe_int",
    "parse_assignment_html",
    "parse_ics_categories",
    "parse_ics_content_line",
    "parse_ics_dt",
    "parse_ics_text",
    "unescape_ics_text",
]
