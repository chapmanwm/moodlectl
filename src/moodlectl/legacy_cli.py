from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import requests

from .client import (
    CONFIG_PATH,
    DEFAULT_BASE_URL,
    MoodleClient,
    MoodleCtlError,
    get_client,
    load_config,
    save_config,
)
from .parsers import (
    clean_html_text,
    decode_web_text,
    extract_assignment_intro,
    extract_id_from_url,
    extract_sesskey,
    find_html_match,
    maybe_int,
    parse_assignment_html,
    parse_ics_categories,
    parse_ics_content_line,
    parse_ics_dt,
    parse_ics_text,
    unescape_ics_text,
)

CACHE_DIR = Path(os.environ["MOODLECTL_CACHE_DIR"]) if os.environ.get("MOODLECTL_CACHE_DIR") else Path.home() / ".local" / "share" / "moodlectl"
DOWNLOADS_DIR = CACHE_DIR / "downloads"
ASSESSMENT_MODULES = {"assign", "quiz", "workshop", "turnitintooltwo"}


def iso(ts: int | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().isoformat(timespec="minutes")


def now_local() -> datetime:
    return datetime.now().astimezone()


def slugify(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "item"


def ensure_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def safe_filename(name: str) -> str:
    name = name.strip().replace("/", "-")
    name = re.sub(r"[^A-Za-z0-9._() -]+", "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:180] or "download"


def canonical_modname(value: str | None) -> str:
    raw = (value or "").strip().lower()
    mapping = {
        "assignment": "assign",
        "assign": "assign",
        "file": "resource",
        "resource": "resource",
        "forum": "forum",
        "quiz": "quiz",
        "external tool": "lti",
        "lti": "lti",
        "page": "page",
        "folder": "folder",
        "attendance": "attendance",
    }
    return mapping.get(raw, raw)


def normalise_course(course: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": course.get("id"),
        "fullname": course.get("fullname") or course.get("fullnamedisplay"),
        "shortname": course.get("shortname"),
        "viewurl": course.get("viewurl"),
        "visible": course.get("visible"),
        "hidden": course.get("hidden"),
        "progress": course.get("progress"),
        "isfavourite": course.get("isfavourite"),
        "startdate": iso(course.get("startdate")),
        "enddate": iso(course.get("enddate")),
    }


def normalise_event(event: dict[str, Any]) -> dict[str, Any]:
    course = event.get("course") or {}
    action = event.get("action") or {}
    return {
        "id": event.get("id"),
        "name": event.get("name"),
        "activityname": event.get("activityname"),
        "activitystr": event.get("activitystr"),
        "modulename": event.get("modulename"),
        "eventtype": event.get("eventtype"),
        "normalised_type": event.get("normalisedeventtype"),
        "normalised_type_text": event.get("normalisedeventtypetext"),
        "formattedtime": clean_html_text(event.get("formattedtime")),
        "overdue": bool(event.get("overdue")),
        "timesort": iso(event.get("timesort")),
        "timestart": iso(event.get("timestart")),
        "description": clean_html_text(event.get("description")),
        "url": event.get("url") or event.get("viewurl"),
        "course": {
            "id": course.get("id"),
            "fullname": course.get("fullname") or course.get("fullnamedisplay"),
            "shortname": course.get("shortname"),
            "viewurl": course.get("viewurl"),
        },
        "action": {
            "displayname": action.get("displayname"),
            "url": action.get("url"),
        },
    }


def timeline_kind_matches(kind: str, event: dict[str, Any]) -> bool:
    if kind == "all":
        return True
    if kind == "assignment":
        return event.get("modulename") == "assign"
    if kind == "assessment":
        return event.get("modulename") in ASSESSMENT_MODULES
    if kind == "overdue":
        return bool(event.get("overdue"))
    return True


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


def print_table(rows: list[dict[str, Any]], columns: list[tuple[str, str]]) -> None:
    if not rows:
        print("No rows.")
        return
    widths = []
    for key, heading in columns:
        width = len(heading)
        for row in rows:
            width = min(max(width, len(str(row.get(key, "") or ""))), 60)
        widths.append(width)
    header = "  ".join(heading.ljust(width) for (_, heading), width in zip(columns, widths))
    print(header)
    print("  ".join("-" * width for width in widths))
    for row in rows:
        cells = []
        for (key, _heading), width in zip(columns, widths):
            value = str(row.get(key, "") or "")
            if len(value) > width:
                value = value[: width - 1] + "…"
            cells.append(value.ljust(width))
        print("  ".join(cells))


def print_kv(data: list[tuple[str, Any]]) -> None:
    width = max((len(key) for key, _ in data), default=0)
    for key, value in data:
        if value is None or value == "":
            continue
        print(f"{key.ljust(width)} : {value}")


def find_course(client: MoodleClient, query: str) -> dict[str, Any]:
    query = query.strip()
    if query.isdigit():
        course_id = int(query)
        for classification in ["inprogress", "allincludinghidden", "all"]:
            courses = client.courses(classification=classification, limit=200).get("courses", [])
            for course in courses:
                if int(course.get("id")) == course_id:
                    return course
        raise MoodleCtlError(f"Could not find course with id {course_id}.")

    courses = client.courses(classification="allincludinghidden", limit=200).get("courses", [])
    lowered = query.lower()
    exact = []
    partial = []
    for course in courses:
        names = [str(course.get("fullname") or ""), str(course.get("shortname") or "")]
        if any(name.lower() == lowered for name in names if name):
            exact.append(course)
        elif any(lowered in name.lower() for name in names if name):
            partial.append(course)
    if exact:
        return exact[0]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        options = ", ".join(f"{c.get('id')}:{c.get('shortname') or c.get('fullname')}" for c in partial[:8])
        raise MoodleCtlError(f"Course query is ambiguous. Matches: {options}")
    raise MoodleCtlError(f"Could not find a course matching {query!r}.")


def course_snapshot(client: MoodleClient, course_query: str, timeline_limit: int = 100) -> dict[str, Any]:
    course_raw = find_course(client, course_query)
    course = normalise_course(course_raw)
    course_id = int(course["id"])
    state = client.course_state(course_id)
    section_lookup = {section["id"]: section for section in ensure_list(state.get("section"))}

    sections: list[dict[str, Any]] = []
    for section in ensure_list(state.get("section")):
        sections.append(
            {
                "id": maybe_int(section.get("id")),
                "number": maybe_int(section.get("number")),
                "title": section.get("title") or section.get("rawtitle"),
                "visible": section.get("visible"),
                "url": section.get("sectionurl"),
                "items": [],
            }
        )
    section_by_id = {str(section["id"]): section for section in sections if section.get("id") is not None}

    activities: list[dict[str, Any]] = []
    resource_items: list[dict[str, Any]] = []
    assignments: list[dict[str, Any]] = []
    forums: list[dict[str, Any]] = []
    quizzes: list[dict[str, Any]] = []
    lti_items: list[dict[str, Any]] = []

    for cm in ensure_list(state.get("cm")):
        section_id = str(cm.get("sectionid"))
        section_meta = section_lookup.get(section_id) or {}
        item = {
            "id": maybe_int(cm.get("id")),
            "name": cm.get("name"),
            "modname": canonical_modname(cm.get("modname")),
            "module_label": cm.get("modname"),
            "url": cm.get("url"),
            "visible": cm.get("visible"),
            "uservisible": cm.get("uservisible"),
            "section_id": maybe_int(cm.get("sectionid")),
            "section_number": maybe_int(cm.get("sectionnumber")),
            "section_title": section_meta.get("title") or section_meta.get("rawtitle"),
        }
        activities.append(item)
        if section_id in section_by_id:
            section_by_id[section_id]["items"].append(item)
        modname = item["modname"]
        if modname == "resource":
            resource_items.append(item)
        elif modname == "assign":
            assignments.append(item)
        elif modname == "forum":
            forums.append(item)
        elif modname == "quiz":
            quizzes.append(item)
        elif modname == "lti":
            lti_items.append(item)

    events = [normalise_event(event) for event in client.course_timeline(course_id, limit=timeline_limit).get("events", [])]
    assignment_urls = {item.get("url") for item in assignments}
    for assignment in assignments:
        matches = [event for event in events if event.get("url") == assignment.get("url")]
        assignment["timeline"] = matches
        if matches:
            first_due = sorted(matches, key=lambda e: e.get("timesort") or "")[0]
            assignment["next_due"] = first_due.get("formattedtime") or first_due.get("timesort")
            assignment["overdue"] = any(event.get("overdue") for event in matches)
        else:
            assignment["next_due"] = None
            assignment["overdue"] = False

    return {
        "fetched_at": now_local().isoformat(timespec="seconds"),
        "course": course,
        "sections": sections,
        "activities": activities,
        "assignments": assignments,
        "resources": resource_items,
        "forums": forums,
        "quizzes": quizzes,
        "external_tools": lti_items,
        "timeline": events,
        "assessment_events": [event for event in events if event.get("modulename") in ASSESSMENT_MODULES],
        "announcement_forum": forums[0] if forums else None,
        "assignment_urls": sorted(url for url in assignment_urls if url),
    }


def parse_assignment_page(client: MoodleClient, assignment_url: str) -> dict[str, Any]:
    html_text = client.get(assignment_url).text
    return parse_assignment_html(client.base_url, assignment_url, html_text)


def due_soon_snapshot(client: MoodleClient, days: int, include_overdue: bool = True, limit: int = 50) -> dict[str, Any]:
    start = now_local()
    end = start + timedelta(days=days)
    source_context = {
        "calendar_export": {"attempted": True, "ok": None, "error": None},
        "timeline_fallback": {"attempted": False, "ok": None, "error": None},
    }

    try:
        calendar = export_calendar(client, mode="courses", period="recentupcoming")
        filtered = []
        for event in calendar["events"]:
            summary = event.get("summary") or ""
            summary_lower = summary.lower()
            if " is due" not in summary_lower and not summary_lower.endswith(" due"):
                continue
            start_iso = event.get("start")
            if not start_iso:
                continue
            try:
                dt = datetime.fromisoformat(start_iso)
            except ValueError:
                continue
            if dt > end:
                continue
            overdue = dt < start
            if overdue and not include_overdue:
                continue
            filtered.append(
                {
                    "id": None,
                    "name": summary,
                    "activityname": summary,
                    "activitystr": None,
                    "modulename": "assign",
                    "eventtype": "calendar",
                    "normalised_type": "calendar",
                    "normalised_type_text": "Calendar",
                    "formattedtime": dt.strftime("%A, %-d %B, %H:%M"),
                    "overdue": overdue,
                    "timesort": start_iso,
                    "timestart": start_iso,
                    "description": event.get("description"),
                    "url": None,
                    "course": {
                        "id": None,
                        "fullname": None,
                        "shortname": ",".join(event.get("categories") or []),
                        "viewurl": None,
                    },
                    "action": {"displayname": None, "url": None},
                }
            )
        filtered.sort(key=lambda e: (e.get("timestart") or "", e.get("course", {}).get("shortname") or "", e.get("name") or ""))
        source_context["calendar_export"]["ok"] = True
        return {
            "fetched_at": start.isoformat(timespec="seconds"),
            "window_days": days,
            "source": "calendar-export",
            "source_context": source_context,
            "events": filtered,
        }
    except Exception as exc:
        source_context["calendar_export"]["ok"] = False
        source_context["calendar_export"]["error"] = str(exc) or exc.__class__.__name__
        source_context["timeline_fallback"]["attempted"] = True
        try:
            events = [normalise_event(event) for event in client.timeline(limit=limit, timesortfrom=0).get("events", [])]
            filtered = []
            for event in events:
                if event.get("modulename") not in ASSESSMENT_MODULES:
                    continue
                ts = event.get("timestart")
                if ts:
                    dt = datetime.fromisoformat(ts)
                    if dt > end:
                        continue
                overdue = bool(event.get("overdue"))
                if not include_overdue and overdue:
                    continue
                if ts:
                    dt = datetime.fromisoformat(ts)
                    if dt < start and not overdue:
                        continue
                filtered.append(event)
            filtered.sort(key=lambda e: (e.get("timestart") or "", e.get("course", {}).get("shortname") or "", e.get("name") or ""))
            source_context["timeline_fallback"]["ok"] = True
            return {
                "fetched_at": start.isoformat(timespec="seconds"),
                "window_days": days,
                "source": "timeline-fallback",
                "source_context": source_context,
                "events": filtered,
            }
        except Exception as fallback_exc:
            source_context["timeline_fallback"]["ok"] = False
            source_context["timeline_fallback"]["error"] = str(fallback_exc) or fallback_exc.__class__.__name__
            raise MoodleCtlError(
                "due-soon failed: calendar export error: "
                f"{source_context['calendar_export']['error']}; timeline fallback error: {source_context['timeline_fallback']['error']}"
            ) from fallback_exc


def resolve_resource(client: MoodleClient, target: str) -> dict[str, Any]:
    if target.startswith("http"):
        resource_url = target
    elif target.isdigit():
        resource_url = f"{client.base_url}/mod/resource/view.php?id={int(target)}"
    else:
        raise MoodleCtlError("Resource must be a Moodle resource id or full resource URL.")

    response = client.get(resource_url, allow_redirects=False)
    download_url = response.headers.get("location")
    if not download_url:
        raise MoodleCtlError("Resource did not redirect to a downloadable file.")
    download_url = urljoin(client.base_url, download_url)
    parsed = urlparse(download_url)
    filename = safe_filename(unquote(Path(parsed.path).name))
    return {
        "resource_url": resource_url,
        "download_url": download_url,
        "filename": filename,
        "resource_id": extract_id_from_url(resource_url),
    }


def download_resource(client: MoodleClient, target: str, out_dir: Path | None = None, filename: str | None = None) -> dict[str, Any]:
    resolved = resolve_resource(client, target)
    out_dir = out_dir or DOWNLOADS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / safe_filename(filename or resolved["filename"])
    response = client.get(resolved["download_url"])
    out_path.write_bytes(response.content)
    return {
        **resolved,
        "saved_to": str(out_path),
        "size_bytes": len(response.content),
        "content_type": response.headers.get("content-type"),
    }


def parse_forum_listing(client: MoodleClient, forum_url: str, limit: int = 20) -> dict[str, Any]:
    html_text = client.get(forum_url).text
    title_match = re.search(r"<title>(.*?)</title>", html_text, re.I | re.S)
    forum_title = clean_html_text(title_match.group(1)) if title_match else None
    discussions = []
    seen = set()
    for match in re.finditer(r'<a[^>]+href="([^"]*/mod/forum/discuss\.php\?d=\d+[^"]*)"[^>]*>(.*?)</a>', html_text, re.I | re.S):
        href = html.unescape(match.group(1))
        href = urljoin(client.base_url, href)
        discussion_id = extract_id_from_url(href, key="d")
        if discussion_id in seen:
            continue
        seen.add(discussion_id)
        title = clean_html_text(match.group(2))
        if not title or re.fullmatch(r"\d{1,2} \w{3} \d{4}", title):
            continue
        discussions.append({"id": discussion_id, "title": title, "url": href})
        if len(discussions) >= limit:
            break
    return {"forum_url": forum_url, "forum_title": forum_title, "discussions": discussions}


def parse_forum_discussion(client: MoodleClient, discussion_url: str) -> dict[str, Any]:
    html_text = client.get(discussion_url).text
    heading_match = re.search(r'<h3 class="discussionname">(.*?)</h3>', html_text, re.I | re.S)
    title = clean_html_text(heading_match.group(1)) if heading_match else None
    posts = []
    for match in re.finditer(r'(<article[^>]*class="forum-post-container[^"]*"[\s\S]*?</article>)', html_text, re.I):
        block = match.group(1)
        post_id_match = re.search(r'data-post-id="(\d+)"', block)
        subject_match = re.search(r'data-region-content="forum-post-core-subject"[^>]*>(.*?)</h3>', block, re.I | re.S)
        author_match = re.search(r'by\s*<a[^>]*>(.*?)</a>\s*-\s*<time[^>]*datetime="([^"]+)"[^>]*>(.*?)</time>', block, re.I | re.S)
        content_match = re.search(r'<div id="post-content-\d+" class="post-content-container">([\s\S]*?)</div>', block, re.I | re.S)
        posts.append(
            {
                "id": maybe_int(post_id_match.group(1)) if post_id_match else None,
                "subject": clean_html_text(subject_match.group(1)) if subject_match else None,
                "author": clean_html_text(author_match.group(1)) if author_match else None,
                "created_at": author_match.group(2) if author_match else None,
                "created_label": clean_html_text(author_match.group(3)) if author_match else None,
                "content": clean_html_text(content_match.group(1)) if content_match else None,
            }
        )
    return {"discussion_url": discussion_url, "title": title, "posts": posts}


def announcement_snapshot(client: MoodleClient, course_snapshot_data: dict[str, Any], limit: int = 5, include_posts: bool = True) -> dict[str, Any]:
    forum = course_snapshot_data.get("announcement_forum")
    if not forum:
        return {"course": course_snapshot_data.get("course"), "forum": None, "discussions": []}
    listing = parse_forum_listing(client, forum["url"], limit=limit)
    if not include_posts:
        return {"course": course_snapshot_data.get("course"), "forum": forum, "discussions": listing["discussions"]}
    discussions = []
    for item in listing["discussions"]:
        detail = parse_forum_discussion(client, item["url"])
        discussions.append({**item, "posts": detail["posts"]})
    return {"course": course_snapshot_data.get("course"), "forum": forum, "discussions": discussions}


def select_course_resources(course_snapshot_data: dict[str, Any], filter_pattern: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
    resources = list(course_snapshot_data.get("resources") or [])
    if filter_pattern:
        regex = re.compile(filter_pattern, re.I)
        resources = [item for item in resources if regex.search(item.get("name") or "")]
    if limit is not None:
        resources = resources[:limit]
    return resources


def fetch_course_resources(
    client: MoodleClient,
    course_snapshot_data: dict[str, Any],
    *,
    filter_pattern: str | None = None,
    limit: int | None = None,
    out_dir: Path | None = None,
) -> dict[str, Any]:
    course = course_snapshot_data["course"]
    course_dir = out_dir or DOWNLOADS_DIR / f"course-{course['id']}-{slugify(course['shortname'] or course['fullname'])}"
    resources = select_course_resources(course_snapshot_data, filter_pattern=filter_pattern, limit=limit)
    results = []
    for item in resources:
        try:
            resolved = resolve_resource(client, str(item.get("id")))
            resolved_name = resolved.get("filename") or f"resource-{item.get('id')}"
            ext = Path(resolved_name).suffix
            base_name = safe_filename(item.get("name") or Path(resolved_name).stem or f"resource-{item.get('id')}")
            filename = f"{base_name}{ext}" if ext and not base_name.lower().endswith(ext.lower()) else (resolved_name if not item.get("name") else base_name)
            results.append(download_resource(client, str(item.get("id")), out_dir=course_dir, filename=filename))
        except Exception as exc:
            results.append({"resource_id": item.get("id"), "name": item.get("name"), "error": str(exc)})
    return {
        "fetched_at": now_local().isoformat(timespec="seconds"),
        "course": course,
        "target_dir": str(course_dir),
        "count": len(results),
        "results": results,
    }


def sync_course_snapshot(
    client: MoodleClient,
    course_query: str,
    *,
    days: int = 14,
    announcement_limit: int = 5,
    include_posts: bool = True,
    fetch_resources: bool = False,
    resource_filter: str | None = None,
    resource_limit: int | None = None,
    out_dir: Path | None = None,
) -> dict[str, Any]:
    course_data = course_snapshot(client, course_query, timeline_limit=50)
    course_id = course_data["course"]["id"]
    course_code = course_data["course"].get("shortname") or course_data["course"].get("fullname") or "course"
    due = due_soon_snapshot(client, days=days, include_overdue=True)
    due_events = []
    for event in due.get("events") or []:
        course_info = event.get("course") or {}
        if course_info.get("id") == course_id:
            due_events.append(event)
            continue
        shortname = course_info.get("shortname") or ""
        fullname = course_info.get("fullname") or ""
        if shortname == course_code or fullname == course_data["course"].get("fullname"):
            due_events.append(event)
    announcements = announcement_snapshot(client, course_data, limit=announcement_limit, include_posts=include_posts)
    fetched_resources = None
    if fetch_resources:
        default_dir = out_dir / "files" if out_dir else None
        fetched_resources = fetch_course_resources(
            client,
            course_data,
            filter_pattern=resource_filter,
            limit=resource_limit,
            out_dir=default_dir,
        )
    return {
        "fetched_at": now_local().isoformat(timespec="seconds"),
        "course": course_data["course"],
        "course_snapshot": course_data,
        "due_soon": due_events,
        "announcements": announcements,
        "fetched_resources": fetched_resources,
    }


def export_calendar(client: MoodleClient, mode: str, period: str) -> dict[str, Any]:
    page = client.get("/calendar/export.php").text
    sesskey = extract_sesskey(page)
    if not sesskey:
        raise MoodleCtlError("Could not find sesskey on calendar export page.")
    payload = {
        "sesskey": sesskey,
        "_qf__core_calendar_export_form": "1",
        "events[exportevents]": mode,
        "period[timeperiod]": period,
        "export": "Export",
    }
    response = client.session.post(f"{client.base_url}/calendar/export.php", data=payload, timeout=client.timeout)
    response.raise_for_status()
    if "text/calendar" not in (response.headers.get("content-type") or "") and "BEGIN:VCALENDAR" not in response.text:
        raise MoodleCtlError("Calendar export did not return ICS data.")
    return {
        "fetched_at": now_local().isoformat(timespec="seconds"),
        "mode": mode,
        "period": period,
        "events": parse_ics_text(response.text),
        "raw_ics": response.text,
    }


def cmd_auth_set_cookie(args: argparse.Namespace) -> int:
    config = load_config()
    config["moodle_session"] = args.cookie
    if args.base_url:
        config["base_url"] = args.base_url.rstrip("/")
    save_config(config)
    print(f"Saved config to {CONFIG_PATH}")
    print("Permissions set to 600.")
    return 0


def probe_auth_status(client: MoodleClient) -> dict[str, Any]:
    response = client.get("/my/")
    html_text = response.text
    title_match = re.search(r"<title>(.*?)</title>", html_text, re.I | re.S)
    title = clean_html_text(title_match.group(1)) if title_match else None
    sesskey = extract_sesskey(html_text)

    response_url = response.url
    parsed_response_url = urlparse(response_url)
    parsed_base_url = urlparse(client.base_url)
    response_path = parsed_response_url.path.rstrip("/") or "/"
    on_dashboard = parsed_response_url.netloc == parsed_base_url.netloc and response_path in {"/my", "/my/index.php"}

    failure_kind = None
    error = None
    if not sesskey:
        if not on_dashboard:
            failure_kind = "redirected"
            error = f"Request was redirected away from the Moodle dashboard to {response_url}."
        else:
            failure_kind = "missing-sesskey"
            error = "Dashboard loaded but sesskey could not be extracted from the HTML."

    return {
        "ok": bool(sesskey),
        "dashboard_url": response_url,
        "dashboard_title": title,
        "sesskey": sesskey,
        "failure_kind": failure_kind,
        "error": error,
    }


def cmd_auth_status(args: argparse.Namespace) -> int:
    config = load_config()
    print(f"Config path: {CONFIG_PATH}")
    print(f"Configured base URL: {config.get('base_url', DEFAULT_BASE_URL)}")
    has_cookie = bool(args.cookie or os.environ.get("MOODLE_SESSION") or config.get("moodle_session"))
    print(f"Cookie available: {'yes' if has_cookie else 'no'}")
    if args.verify:
        if not has_cookie:
            print("Live auth check: skipped (no cookie configured)")
            return 0
        status = probe_auth_status(get_client(args))
        print(f"Live auth check: {'ok' if status['ok'] else 'failed'}")
        if status.get("dashboard_title"):
            print(f"Dashboard title: {status['dashboard_title']}")
        if status.get("dashboard_url"):
            print(f"Dashboard URL: {status['dashboard_url']}")
        if status.get("sesskey"):
            print("Sesskey available: yes")
        if status.get("failure_kind"):
            print(f"Failure kind: {status['failure_kind']}")
        if status.get("error"):
            print(f"Auth error: {status['error']}")
    return 0


def cmd_courses(args: argparse.Namespace) -> int:
    client = get_client(args)
    data = client.courses(classification=args.classification, limit=args.limit, offset=args.offset, sort=args.sort)
    courses = [normalise_course(course) for course in data.get("courses", [])]
    if args.json:
        print(json.dumps(courses, indent=2))
    else:
        print_table(
            courses,
            [
                ("id", "id"),
                ("fullname", "course"),
                ("progress", "progress"),
                ("startdate", "start"),
                ("enddate", "end"),
            ],
        )
    if args.cache:
        write_json(Path(args.cache), courses)
        print(f"\nWrote {len(courses)} courses to {args.cache}")
    return 0


def cmd_timeline(args: argparse.Namespace) -> int:
    client = get_client(args)
    timesortfrom = int(now_local().timestamp()) if args.from_now else args.timesortfrom
    data = client.timeline(limit=args.limit, timesortfrom=timesortfrom, courseid=args.course_id)
    events = [normalise_event(event) for event in data.get("events", [])]
    events = [event for event in events if timeline_kind_matches(args.kind, event)]
    if args.json:
        print(json.dumps(events, indent=2))
    else:
        rows = []
        for event in events:
            rows.append(
                {
                    "when": event.get("formattedtime") or event.get("timesort") or "",
                    "course": (event.get("course") or {}).get("shortname") or (event.get("course") or {}).get("fullname") or "",
                    "type": event.get("modulename") or "",
                    "event": event.get("name") or "",
                    "overdue": "yes" if event.get("overdue") else "",
                }
            )
        print_table(
            rows,
            [
                ("when", "when"),
                ("course", "course"),
                ("type", "type"),
                ("event", "event"),
                ("overdue", "overdue"),
            ],
        )
    if args.cache:
        write_json(Path(args.cache), events)
        print(f"\nWrote {len(events)} events to {args.cache}")
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    client = get_client(args)
    courses = [
        normalise_course(course)
        for course in client.courses(classification=args.classification, limit=args.course_limit).get("courses", [])
    ]
    events = [
        normalise_event(event)
        for event in client.timeline(
            limit=args.timeline_limit,
            timesortfrom=int(now_local().timestamp()) if args.from_now else 0,
        ).get("events", [])
    ]
    if args.kind != "all":
        events = [event for event in events if timeline_kind_matches(args.kind, event)]
    payload = {
        "fetched_at": now_local().isoformat(timespec="seconds"),
        "base_url": client.base_url,
        "courses": courses,
        "timeline": events,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print("Courses")
        print("=======")
        print_table(courses, [("id", "id"), ("fullname", "course"), ("progress", "progress")])
        print("\nTimeline")
        print("========")
        rows = []
        for event in events:
            rows.append(
                {
                    "when": event.get("formattedtime") or event.get("timesort") or "",
                    "course": (event.get("course") or {}).get("shortname") or (event.get("course") or {}).get("fullname") or "",
                    "type": event.get("modulename") or "",
                    "event": event.get("name") or "",
                }
            )
        print_table(rows, [("when", "when"), ("course", "course"), ("type", "type"), ("event", "event")])
    if args.cache_snapshot:
        snapshot_path = CACHE_DIR / "latest-dashboard.json"
        write_json(snapshot_path, payload)
        print(f"\nWrote dashboard snapshot to {snapshot_path}")
    return 0


def cmd_course(args: argparse.Namespace) -> int:
    client = get_client(args)
    snapshot = course_snapshot(client, args.course, timeline_limit=args.timeline_limit)
    if args.json:
        print(json.dumps(snapshot, indent=2))
    else:
        course = snapshot["course"]
        print(course["fullname"])
        print("=" * len(course["fullname"]))
        print_kv(
            [
                ("id", course.get("id")),
                ("shortname", course.get("shortname")),
                ("url", course.get("viewurl")),
                ("start", course.get("startdate")),
                ("end", course.get("enddate")),
                ("resources", len(snapshot["resources"])),
                ("assignments", len(snapshot["assignments"])),
                ("forums", len(snapshot["forums"])),
                ("quizzes", len(snapshot["quizzes"])),
                ("external tools", len(snapshot["external_tools"])),
            ]
        )
        print("\nAssignments")
        print("-----------")
        print_table(
            [
                {
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "section": item.get("section_title") or "",
                    "due": item.get("next_due") or "",
                    "overdue": "yes" if item.get("overdue") else "",
                }
                for item in snapshot["assignments"]
            ],
            [("id", "id"), ("name", "name"), ("section", "section"), ("due", "due"), ("overdue", "overdue")],
        )
        print("\nResources")
        print("---------")
        print_table(
            [
                {"id": item.get("id"), "name": item.get("name"), "section": item.get("section_title") or ""}
                for item in snapshot["resources"][:20]
            ],
            [("id", "id"), ("name", "name"), ("section", "section")],
        )
        if len(snapshot["resources"]) > 20:
            print(f"... and {len(snapshot['resources']) - 20} more resources")
        if snapshot["forums"]:
            print("\nForums")
            print("------")
            print_table(
                [{"id": item.get("id"), "name": item.get("name"), "section": item.get("section_title") or ""} for item in snapshot["forums"]],
                [("id", "id"), ("name", "name"), ("section", "section")],
            )
    if args.cache:
        write_json(Path(args.cache), snapshot)
        print(f"\nWrote course snapshot to {args.cache}")
    return 0


def cmd_assignment(args: argparse.Namespace) -> int:
    client = get_client(args)
    target = args.assignment
    if target.isdigit():
        assignment_url = f"{client.base_url}/mod/assign/view.php?id={int(target)}"
    elif target.startswith("http"):
        assignment_url = target
    else:
        raise MoodleCtlError("Assignment must be a Moodle assignment id or full assignment URL.")
    snapshot = parse_assignment_page(client, assignment_url)
    if args.json:
        print(json.dumps(snapshot, indent=2))
    else:
        print(snapshot.get("name") or snapshot.get("heading") or "Assignment")
        print("=" * len(snapshot.get("name") or snapshot.get("heading") or "Assignment"))
        course = snapshot.get("course") or {}
        print_kv(
            [
                ("id", snapshot.get("id")),
                ("course", course.get("fullname")),
                ("url", snapshot.get("url")),
                ("primary action", snapshot.get("primary_action")),
            ]
        )
        if snapshot.get("status"):
            print("\nStatus")
            print("------")
            print_kv(list(snapshot["status"].items()))
        if snapshot.get("description"):
            print("\nDescription")
            print("-----------")
            print(snapshot["description"])
        if snapshot.get("links"):
            print("\nRelevant links")
            print("--------------")
            for link in snapshot["links"][:12]:
                print(f"- {link['text']}: {link['url']}")
    if args.cache:
        write_json(Path(args.cache), snapshot)
        print(f"\nWrote assignment snapshot to {args.cache}")
    return 0


def cmd_due_soon(args: argparse.Namespace) -> int:
    client = get_client(args)
    snapshot = due_soon_snapshot(client, days=args.days, include_overdue=not args.exclude_overdue, limit=args.limit)
    if args.json:
        print(json.dumps(snapshot, indent=2))
    else:
        print(f"Source: {snapshot.get('source') or 'unknown'}")
        calendar_context = (snapshot.get("source_context") or {}).get("calendar_export") or {}
        if calendar_context.get("attempted") and calendar_context.get("ok") is False and calendar_context.get("error"):
            print(f"Calendar export error: {calendar_context['error']}")
        rows = []
        for event in snapshot["events"]:
            rows.append(
                {
                    "when": event.get("formattedtime") or event.get("timesort") or "",
                    "course": (event.get("course") or {}).get("shortname") or (event.get("course") or {}).get("fullname") or "",
                    "type": event.get("modulename") or "",
                    "event": event.get("name") or "",
                    "overdue": "yes" if event.get("overdue") else "",
                }
            )
        print_table(rows, [("when", "when"), ("course", "course"), ("type", "type"), ("event", "event"), ("overdue", "overdue")])
    if args.cache:
        write_json(Path(args.cache), snapshot)
        print(f"\nWrote due-soon snapshot to {args.cache}")
    return 0


def cmd_calendar_export(args: argparse.Namespace) -> int:
    client = get_client(args)
    snapshot = export_calendar(client, mode=args.events, period=args.period)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(snapshot["raw_ics"])
        print(f"Wrote ICS to {out}")
    if args.json:
        printable = dict(snapshot)
        printable.pop("raw_ics", None)
        print(json.dumps(printable, indent=2))
    else:
        print(f"Calendar export: {args.events} / {args.period}")
        print(f"Events: {len(snapshot['events'])}")
        rows = []
        for event in snapshot["events"][:20]:
            rows.append({"start": event.get("dtstart") or "", "summary": event.get("summary") or "", "location": event.get("location") or ""})
        print_table(rows, [("start", "start"), ("summary", "summary"), ("location", "location")])
        if len(snapshot["events"]) > 20:
            print(f"... and {len(snapshot['events']) - 20} more calendar events")
    if args.cache:
        write_json(Path(args.cache), {k: v for k, v in snapshot.items() if k != "raw_ics"})
        print(f"\nWrote calendar snapshot to {args.cache}")
    return 0


def cmd_resource_fetch(args: argparse.Namespace) -> int:
    client = get_client(args)
    out_dir = Path(args.out_dir) if args.out_dir else DOWNLOADS_DIR
    result = download_resource(client, args.resource, out_dir=out_dir, filename=args.filename)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print_kv([
            ("resource id", result.get("resource_id")),
            ("resource url", result.get("resource_url")),
            ("download url", result.get("download_url")),
            ("saved to", result.get("saved_to")),
            ("size bytes", result.get("size_bytes")),
            ("content type", result.get("content_type")),
        ])
    return 0


def cmd_course_fetch(args: argparse.Namespace) -> int:
    client = get_client(args)
    snapshot = course_snapshot(client, args.course, timeline_limit=50)
    payload = fetch_course_resources(
        client,
        snapshot,
        filter_pattern=args.filter,
        limit=args.limit,
        out_dir=Path(args.out_dir) if args.out_dir else None,
    )
    course = payload["course"]
    results = payload["results"]
    course_dir = Path(payload["target_dir"])
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"Fetched {len(results)} resources for {course['fullname']}")
        print(f"Target dir: {course_dir}")
        rows = []
        for item in results[:30]:
            rows.append({
                "id": item.get("resource_id") or "",
                "file": Path(item.get("saved_to") or item.get("name") or item.get("error") or "").name,
                "size": item.get("size_bytes") or "",
                "status": "ok" if not item.get("error") else item.get("error"),
            })
        print_table(rows, [("id", "id"), ("file", "file"), ("size", "size"), ("status", "status")])
        if len(results) > 30:
            print(f"... and {len(results) - 30} more")
    if args.cache:
        write_json(Path(args.cache), payload)
        print(f"\nWrote fetch snapshot to {args.cache}")
    return 0


def cmd_announcements(args: argparse.Namespace) -> int:
    client = get_client(args)
    snapshot = course_snapshot(client, args.course, timeline_limit=20)
    payload = announcement_snapshot(client, snapshot, limit=args.limit, include_posts=args.include_posts)
    forum = payload.get("forum")
    if not forum:
        raise MoodleCtlError("No forum found for this course.")
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"Announcements for {snapshot['course']['fullname']}")
        print("=" * (18 + len(snapshot['course']['fullname'])))
        rows = []
        for item in payload["discussions"]:
            rows.append({
                "id": item.get("id"),
                "title": item.get("title"),
                "posts": len(item.get("posts") or []),
                "url": item.get("url"),
            })
        print_table(rows, [("id", "id"), ("title", "title"), ("posts", "posts"), ("url", "url")])
        if args.include_posts:
            for item in payload["discussions"][:3]:
                posts = item.get("posts") or []
                if not posts:
                    continue
                print(f"\n{item['title']}")
                print("-" * len(item['title']))
                first = posts[0]
                print_kv([("author", first.get("author")), ("created", first.get("created_label"))])
                if first.get("content"):
                    print(first["content"][:1200])
    if args.cache:
        write_json(Path(args.cache), payload)
        print(f"\nWrote announcements snapshot to {args.cache}")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    client = get_client(args)
    base_dir = Path(args.out_dir) if args.out_dir else CACHE_DIR / f"sync-{slugify(args.course)}"
    base_dir.mkdir(parents=True, exist_ok=True)
    payload = sync_course_snapshot(
        client,
        args.course,
        days=args.days,
        announcement_limit=args.announcement_limit,
        include_posts=not args.no_posts,
        fetch_resources=args.fetch_resources,
        resource_filter=args.resource_filter,
        resource_limit=args.resource_limit,
        out_dir=base_dir,
    )

    manifest_path = base_dir / "manifest.json"
    course_snapshot_path = base_dir / "course.json"
    announcements_path = base_dir / "announcements.json"
    due_path = base_dir / "due-soon.json"

    write_json(course_snapshot_path, payload["course_snapshot"])
    write_json(announcements_path, payload["announcements"])
    write_json(due_path, payload["due_soon"])

    manifest = {
        "fetched_at": payload["fetched_at"],
        "course": payload["course"],
        "paths": {
            "base_dir": str(base_dir),
            "course_snapshot": str(course_snapshot_path),
            "announcements": str(announcements_path),
            "due_soon": str(due_path),
            "manifest": str(manifest_path),
        },
        "counts": {
            "assignments": len(payload["course_snapshot"].get("assignments") or []),
            "resources": len(payload["course_snapshot"].get("resources") or []),
            "due_soon": len(payload.get("due_soon") or []),
            "announcements": len((payload.get("announcements") or {}).get("discussions") or []),
            "fetched_resources": len(((payload.get("fetched_resources") or {}).get("results") or [])),
        },
        "files_dir": str(base_dir / "files") if payload.get("fetched_resources") else None,
    }
    if payload.get("fetched_resources"):
        fetch_path = base_dir / "fetched-resources.json"
        write_json(fetch_path, payload["fetched_resources"])
        manifest["paths"]["fetched_resources"] = str(fetch_path)
    write_json(manifest_path, manifest)

    if args.json:
        print(json.dumps({**payload, "manifest": manifest}, indent=2))
    else:
        print(f"Synced {payload['course']['fullname']}")
        print(f"Base dir: {base_dir}")
        print_kv([
            ("assignments", manifest['counts']['assignments']),
            ("resources", manifest['counts']['resources']),
            ("due soon", manifest['counts']['due_soon']),
            ("announcements", manifest['counts']['announcements']),
            ("fetched resources", manifest['counts']['fetched_resources']),
            ("course snapshot", course_snapshot_path),
            ("announcements file", announcements_path),
            ("due-soon file", due_path),
            ("manifest", manifest_path),
        ])
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    client = get_client(args)
    payload = {
        "fetched_at": now_local().isoformat(timespec="seconds"),
        "base_url": client.base_url,
        "courses": [
            normalise_course(course)
            for course in client.courses(classification=args.classification, limit=args.course_limit).get("courses", [])
        ],
        "timeline": [
            normalise_event(event)
            for event in client.timeline(limit=args.timeline_limit, timesortfrom=int(now_local().timestamp()) if args.from_now else 0).get("events", [])
        ],
    }
    out = Path(args.out)
    write_json(out, payload)
    print(f"Wrote snapshot to {out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="moodlectl",
        description="Read Moodle dashboard context from the terminal.",
        epilog=(
            "Auth lookup order: --cookie, MOODLE_SESSION, then ~/.config/moodlectl/config.json. "
            "This works well on SSH/VPS sessions after copying a MoodleSession cookie from a browser once."
        ),
    )
    parser.add_argument("--base-url", default=None, help=f"Moodle base URL (default: {DEFAULT_BASE_URL})")
    parser.add_argument("--cookie", default=None, help="Override MoodleSession cookie for this command")
    parser.add_argument("--timeout", type=int, default=30, help="HTTP timeout in seconds")

    subparsers = parser.add_subparsers(dest="command", required=True)

    auth = subparsers.add_parser("auth", help="Manage local auth config")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)

    auth_set = auth_sub.add_parser("set-cookie", help="Save a MoodleSession cookie to local config")
    auth_set.add_argument("cookie", help="Value of the MoodleSession cookie copied from a browser session")
    auth_set.add_argument("--base-url", default=None, help="Persist a custom Moodle base URL")
    auth_set.set_defaults(func=cmd_auth_set_cookie)

    auth_status = auth_sub.add_parser("status", help="Show auth config status")
    auth_status.add_argument("--verify", action="store_true", help="Also probe /my/ and report whether the cookie is live and sesskey extraction works")
    auth_status.set_defaults(func=cmd_auth_status)

    courses = subparsers.add_parser("courses", help="List courses from the dashboard overview")
    courses.add_argument("--classification", default="inprogress", choices=["all", "allincludinghidden", "favourites", "hidden", "inprogress", "future", "past", "customfield"], help="Course classification filter")
    courses.add_argument("--sort", default="fullname", choices=["fullname", "shortname", "lastaccess", "idnumber"], help="Course sorting")
    courses.add_argument("--limit", type=int, default=50)
    courses.add_argument("--offset", type=int, default=0)
    courses.add_argument("--json", action="store_true")
    courses.add_argument("--cache", default=None, help="Optional file path to write JSON output")
    courses.set_defaults(func=cmd_courses)

    timeline = subparsers.add_parser("timeline", help="List Moodle timeline events")
    timeline.add_argument("--kind", default="assessment", choices=["all", "assignment", "assessment", "overdue"], help="Filter event types")
    timeline.add_argument("--limit", type=int, default=25)
    timeline.add_argument("--timesortfrom", type=int, default=0, help="Unix timestamp lower bound")
    timeline.add_argument("--from-now", action="store_true", help="Only include events from now onward")
    timeline.add_argument("--course-id", type=int, default=None, help="Optional Moodle course id")
    timeline.add_argument("--json", action="store_true")
    timeline.add_argument("--cache", default=None, help="Optional file path to write JSON output")
    timeline.set_defaults(func=cmd_timeline)

    dashboard = subparsers.add_parser("dashboard", help="Fetch both course overview and timeline")
    dashboard.add_argument("--classification", default="inprogress", choices=["all", "allincludinghidden", "favourites", "hidden", "inprogress", "future", "past", "customfield"])
    dashboard.add_argument("--course-limit", type=int, default=50)
    dashboard.add_argument("--timeline-limit", type=int, default=25)
    dashboard.add_argument("--kind", default="assessment", choices=["all", "assignment", "assessment", "overdue"])
    dashboard.add_argument("--from-now", action="store_true", help="Only include events from now onward")
    dashboard.add_argument("--json", action="store_true")
    dashboard.add_argument("--cache-snapshot", action="store_true", help="Write ~/.local/share/moodlectl/latest-dashboard.json")
    dashboard.set_defaults(func=cmd_dashboard)

    course = subparsers.add_parser("course", help="Show per-course structure and assessment context")
    course.add_argument("course", help="Course id or case-insensitive name fragment")
    course.add_argument("--timeline-limit", type=int, default=100)
    course.add_argument("--json", action="store_true")
    course.add_argument("--cache", default=None, help="Optional file path to write JSON output")
    course.set_defaults(func=cmd_course)

    assignment = subparsers.add_parser("assignment", help="Show assignment details from a Moodle assignment page")
    assignment.add_argument("assignment", help="Assignment id or full mod/assign URL")
    assignment.add_argument("--json", action="store_true")
    assignment.add_argument("--cache", default=None, help="Optional file path to write JSON output")
    assignment.set_defaults(func=cmd_assignment)

    due_soon = subparsers.add_parser("due-soon", help="List upcoming assessment deadlines")
    due_soon.add_argument("--days", type=int, default=7, help="Window size in days")
    due_soon.add_argument("--limit", type=int, default=50, help="Max timeline events to inspect (Moodle caps this at 50)")
    due_soon.add_argument("--exclude-overdue", action="store_true")
    due_soon.add_argument("--json", action="store_true")
    due_soon.add_argument("--cache", default=None, help="Optional file path to write JSON output")
    due_soon.set_defaults(func=cmd_due_soon)

    calendar_export_parser = subparsers.add_parser("calendar-export", help="Export Moodle calendar ICS and parse it")
    calendar_export_parser.add_argument("--events", default="all", choices=["all", "categories", "courses", "groups", "user"])
    calendar_export_parser.add_argument("--period", default="recentupcoming", choices=["weeknow", "monthnow", "recentupcoming", "custom"])
    calendar_export_parser.add_argument("--json", action="store_true")
    calendar_export_parser.add_argument("--out", default=None, help="Optional path to save the raw ICS")
    calendar_export_parser.add_argument("--cache", default=None, help="Optional file path to write parsed JSON output")
    calendar_export_parser.set_defaults(func=cmd_calendar_export)

    fetch_parser = subparsers.add_parser("fetch", help="Download Moodle course resources")
    fetch_sub = fetch_parser.add_subparsers(dest="fetch_command", required=True)

    fetch_resource = fetch_sub.add_parser("resource", help="Download a single Moodle file resource by id or URL")
    fetch_resource.add_argument("resource", help="Resource id or full mod/resource URL")
    fetch_resource.add_argument("--out-dir", default=None, help="Directory to save the file into")
    fetch_resource.add_argument("--filename", default=None, help="Optional output filename override")
    fetch_resource.add_argument("--json", action="store_true")
    fetch_resource.set_defaults(func=cmd_resource_fetch)

    fetch_course = fetch_sub.add_parser("course-files", help="Download resource files for a course")
    fetch_course.add_argument("course", help="Course id or name fragment")
    fetch_course.add_argument("--filter", default=None, help="Regex filter applied to resource names")
    fetch_course.add_argument("--limit", type=int, default=None, help="Optional max number of resources to fetch")
    fetch_course.add_argument("--out-dir", default=None, help="Directory to save files into")
    fetch_course.add_argument("--json", action="store_true")
    fetch_course.add_argument("--cache", default=None, help="Optional file path to write JSON output")
    fetch_course.set_defaults(func=cmd_course_fetch)

    announcements = subparsers.add_parser("announcements", help="List announcement discussions for a course forum")
    announcements.add_argument("course", help="Course id or name fragment")
    announcements.add_argument("--limit", type=int, default=10)
    announcements.add_argument("--include-posts", action="store_true", help="Also fetch the post bodies for each discussion")
    announcements.add_argument("--json", action="store_true")
    announcements.add_argument("--cache", default=None, help="Optional file path to write JSON output")
    announcements.set_defaults(func=cmd_announcements)

    sync = subparsers.add_parser("sync", help="Sync a course into a self-contained local bundle")
    sync.add_argument("course", help="Course id or name fragment")
    sync.add_argument("--days", type=int, default=14, help="Due-soon window in days")
    sync.add_argument("--announcement-limit", type=int, default=5)
    sync.add_argument("--no-posts", action="store_true", help="Skip fetching announcement post bodies")
    sync.add_argument("--fetch-resources", action="store_true", help="Also download selected course resources")
    sync.add_argument("--resource-filter", default=None, help="Regex filter for resource names when fetching")
    sync.add_argument("--resource-limit", type=int, default=None, help="Limit number of fetched resources")
    sync.add_argument("--out-dir", default=None, help="Output directory for the sync bundle")
    sync.add_argument("--json", action="store_true")
    sync.set_defaults(func=cmd_sync)

    export_cmd = subparsers.add_parser("export", help="Write a full dashboard snapshot to JSON")
    export_cmd.add_argument("--classification", default="inprogress", choices=["all", "allincludinghidden", "favourites", "hidden", "inprogress", "future", "past", "customfield"])
    export_cmd.add_argument("--course-limit", type=int, default=50)
    export_cmd.add_argument("--timeline-limit", type=int, default=50)
    export_cmd.add_argument("--from-now", action="store_true")
    export_cmd.add_argument("--out", default=str(CACHE_DIR / "latest-dashboard.json"))
    export_cmd.set_defaults(func=cmd_export)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except requests.HTTPError as exc:
        print(f"HTTP error: {exc}", file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print(f"Network error: {exc}", file=sys.stderr)
        return 1
    except MoodleCtlError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
