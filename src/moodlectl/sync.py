"""Course sync engine: mirror a Moodle course into a readable, diffable local folder."""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from .render import VIDEO_EXT, AUDIO_EXT, extract_text, html_to_md, main_region, page_md, safe_name
from .web import AuthExpired, Moodle, MoodleError, OffSite, filename_from_url

UK = ZoneInfo("Europe/London")
CODE_RE = re.compile(r"\b([A-Z]{3,5}\d{3,5}[A-Z]?)\b")
PAGE_MODULES = {"page", "quiz", "book", "lesson", "choice", "feedback", "workshop", "glossary", "wiki", "h5pactivity", "scorm", "data"}
FORUM_DISCUSSIONS = 12


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone(UK).isoformat(timespec="seconds")


def corpus_root() -> Path:
    env = os.environ.get("MOODLECTL_CORPUS")
    if env:
        return Path(env)
    return Path.home() / ".local" / "share" / "moodlectl" / "corpus"


def meta_dir() -> Path:
    d = corpus_root() / "_moodle"
    d.mkdir(parents=True, exist_ok=True)
    return d


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)


def course_code(course: dict[str, Any]) -> str | None:
    for field in ("shortname", "fullname"):
        m = CODE_RE.search(str(course.get(field) or ""))
        if m:
            return m.group(1)
    return None


def parse_uk_datetime(text: str) -> datetime | None:
    text = re.sub(r"\s+", " ", text).strip().rstrip(".")
    for fmt in ("%A, %d %B %Y, %I:%M %p", "%A, %d %B %Y, %H:%M", "%d %B %Y, %I:%M %p", "%A, %d %B %Y"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UK)
        except ValueError:
            continue
    return None


def activity_dates(html: str) -> dict[str, str]:
    """{'Opened': iso, 'Due': iso, 'Closes': iso, ...} from the activity header."""
    soup = BeautifulSoup(html, "lxml")
    out: dict[str, str] = {}
    box = soup.select_one("[data-region=activity-dates]") or soup.select_one(".activity-dates")
    if not box:
        return out
    text = box.get_text("\n")
    for m in re.finditer(r"(Opens|Opened|Due|Closes|Closed|Cut-off date|Available from|Available until):\s*\n?\s*([^\n]+)", text):
        dt = parse_uk_datetime(m.group(2))
        out[m.group(1)] = dt.isoformat() if dt else m.group(2).strip()
    return out


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
class CourseSync:
    def __init__(self, m: Moodle, code: str, course_id: int, name: str, *, videos: bool = False,
                 max_mb: int = 150, log: Callable[[str], None] = print) -> None:
        self.m, self.code, self.cid, self.name = m, code, course_id, name
        self.videos, self.max_bytes, self.log = videos, max_mb * 1_000_000, log
        self.dir = corpus_root() / code / "moodle"
        self.files_dir, self.text_dir, self.pages_dir = self.dir / "files", self.dir / "text", self.dir / "pages"
        self.prev = read_json(self.dir / "index.json", {}) or {}
        self.prev_items: dict[str, Any] = self.prev.get("items", {})
        self.changes: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.stats = {"downloaded": 0, "unchanged": 0, "skipped": 0}
        self.used_paths: dict[Path, str] = {}

    # -- helpers -----------------------------------------------------------
    def change(self, kind: str, title: str, **extra: Any) -> None:
        self.changes.append({"at": now_iso(), "course": self.code, "kind": kind, "title": title, **extra})

    def rel(self, p: Path) -> str:
        return str(p.relative_to(self.dir))

    def fetch_file(self, url: str, dest: Path, label: str, prev_files: dict[str, Any]) -> dict[str, Any]:
        """Download url to dest unless an identical revision is already on disk."""
        n = 2
        while self.used_paths.get(dest, url) != url:  # two Moodle items with the same name
            dest = dest.with_name(f"{dest.stem.rsplit(' (', 1)[0]} ({n}){dest.suffix}")
            n += 1
        self.used_paths[dest] = url
        ext = dest.suffix.lower().lstrip(".")
        rec: dict[str, Any] = {"path": self.rel(dest), "url": url}
        old = prev_files.get(rec["path"])
        if old and old.get("url") == url and dest.exists():
            self.stats["unchanged"] += 1
            if not old.get("text"):  # backfill if an extractor became available since the last sync
                return {**old, **self.write_text(dest, old["path"])}
            return {**old}
        if ext in VIDEO_EXT | AUDIO_EXT and not self.videos:
            self.stats["skipped"] += 1
            return {**rec, "skipped": "video/audio (fetch with: moodlectl get '<url>')"}
        try:
            res = self.m.download(url, dest, max_bytes=self.max_bytes)
        except AuthExpired:
            raise
        except OffSite as exc:
            return {**rec, "skipped": f"external link: {str(exc).split(': ', 1)[-1]}"}
        except Exception as exc:
            self.errors.append(f"{label}: {exc}")
            return {**rec, "error": str(exc)}
        if res.get("skipped"):
            self.stats["skipped"] += 1
            return {**rec, **res}
        self.stats["downloaded"] += 1
        rec["size"] = res["size"]
        rec.update(self.write_text(dest, rec["path"]))
        self.change("updated_file" if old else "new_file", f"{label}: {dest.name}", path=rec["path"])
        return rec

    def write_text(self, dest: Path, rel_path: str) -> dict[str, str]:
        txt = extract_text(dest)
        if not (txt and txt.strip()):
            return {}
        tpath = self.text_dir / Path(rel_path).relative_to("files").with_suffix(".txt")
        tpath.parent.mkdir(parents=True, exist_ok=True)
        tpath.write_text(txt)
        return {"text": self.rel(tpath)}

    def file_dest(self, folder: str | None, filename: str) -> Path:
        base = self.files_dir / safe_name(folder) if folder else self.files_dir
        parts = [safe_name(p) for p in Path(filename).parts]
        return base.joinpath(*parts)

    def pluginfile_links(self, html: str, must_contain: str | None = None) -> list[tuple[str, str]]:
        region = main_region(html)
        seen, out = set(), []
        for a in region.select("a[href*='pluginfile.php']"):
            href = a["href"].split("#")[0]
            if must_contain and must_contain not in href:
                continue
            clean = re.sub(r"[?&]forcedownload=1", "", href)
            if clean in seen:
                continue
            seen.add(clean)
            out.append((href, a.get_text(" ", strip=True)))
        return out

    # -- module handlers -----------------------------------------------------
    def do_resource(self, item: dict, prev_files: dict) -> None:
        loc = self.m.head_location(item["url"])
        if loc and "pluginfile.php" not in loc and urlparse(loc).netloc != self.m.host:
            item["target"], item["note"] = loc, "external link"
            return
        if not loc or "pluginfile.php" not in loc:
            html = self.m.html(item["url"])
            links = self.pluginfile_links(html)
            if not links:
                item["note"] = "no downloadable file (embedded media or external)"
                if loc:
                    item["target"] = loc
                return
            loc = links[0][0]
        fname = filename_from_url(loc)
        ext = Path(fname).suffix
        base = safe_name(item["name"])
        name = base if base.lower().endswith(ext.lower()) else base + ext
        item["files"] = [self.fetch_file(loc, self.file_dest(None, name), item["name"], prev_files)]

    def do_folder(self, item: dict, prev_files: dict) -> None:
        html = self.m.html(item["url"])
        files = []
        for href, _ in self.pluginfile_links(html, "/mod_folder/content/"):
            path = unquote(urlparse(href).path)
            rel = re.sub(r"^.*/mod_folder/content/\d+/", "", path)
            files.append(self.fetch_file(href, self.file_dest(item["name"], rel), item["name"], prev_files))
        intro = main_region(html).select_one(".box.generalbox, #intro")
        if intro and intro.get_text(strip=True):
            item["description"] = html_to_md(str(intro))
        item["files"] = files

    def do_assign(self, item: dict, prev_files: dict) -> None:
        from .parsers import parse_assignment_html

        html = self.m.html(item["url"])
        parsed = parse_assignment_html(self.m.base_url, item["url"], html)
        status = {k: v for k, v in (parsed.get("status") or {}).items()}
        dates = activity_dates(html)
        item["dates"] = dates
        item["status"] = status
        files = []
        for href, _ in self.pluginfile_links(html, "/mod_assign/introattachment/"):
            files.append(self.fetch_file(href, self.file_dest(item["name"], filename_from_url(href)), item["name"], prev_files))
        item["files"] = files
        region = main_region(html)
        for sel in (".submissionstatustable", ".feedback", "form"):
            for el in region.select(sel):
                el.decompose()
        body = html_to_md(str(region))
        md = [f"# {item['name']}", "", f"Moodle: {item['url']}", ""]
        if dates:
            md += [f"- **{k}:** {fmt_dt(v)}" for k, v in dates.items()] + [""]
        if status:
            md += ["## Submission status", ""] + [f"- **{k}:** {v}" for k, v in status.items()] + [""]
        if files:
            md += ["## Attached briefs", ""] + [f"- {f['path']}" + (f" (text: {f['text']})" if f.get('text') else "") for f in files] + [""]
        md += ["## Page", "", body]
        p = self.pages_dir / f"{safe_name(item['name'])}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(md) + "\n")
        item["page"] = self.rel(p)
        write_json(self.dir / "assignments" / f"{item['id']}.json", {"fetched_at": now_iso(), **parsed, "dates": dates})

    def do_page(self, item: dict, prev_files: dict) -> None:
        html = self.m.html(item["url"])
        item["dates"] = activity_dates(html)
        files = [self.fetch_file(h, self.file_dest(item["name"], filename_from_url(h)), item["name"], prev_files)
                 for h, _ in self.pluginfile_links(html)]
        item["files"] = files
        md = page_md(html)
        p = self.pages_dir / f"{safe_name(item['name'])}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        new = f"# {item['name']}\n\nMoodle: {item['url']}\n\n{md}\n"
        old_hash = (self.prev_items.get(str(item["id"])) or {}).get("page_hash")
        item["page_hash"] = sha(md)
        if old_hash and old_hash != item["page_hash"]:
            self.change("page_updated", item["name"], path=self.rel(p))
        p.write_text(new)
        item["page"] = self.rel(p)

    def do_url(self, item: dict, prev_files: dict) -> None:
        loc = self.m.head_location(item["url"])
        if not loc:
            html = self.m.html(item["url"])
            a = main_region(html).select_one(".urlworkaround a, a[href^='http']")
            loc = a["href"] if a else None
        item["target"] = loc

    def do_forum(self, item: dict) -> list[dict[str, Any]]:
        from .legacy_cli import parse_forum_discussion, parse_forum_listing

        listing = parse_forum_listing(self.m, item["url"], limit=FORUM_DISCUSSIONS)
        prev_disc = {str(d["id"]): d for d in (self.prev.get("announcements") or []) if d.get("forum_id") == item["id"]}
        out = []
        for i, d in enumerate(listing.get("discussions", [])):
            key = str(d["id"])
            old = prev_disc.get(key)
            if old and i >= 4:  # older discussions: reuse stored posts
                out.append(old)
                continue
            detail = parse_forum_discussion(self.m, d["url"])
            disc = {"forum_id": item["id"], "forum": item["name"], "id": d["id"], "title": d["title"], "url": d["url"],
                    "posts": detail.get("posts", [])}
            if not old:
                if self.prev:
                    first = (disc["posts"] or [{}])[0]
                    self.change("new_announcement", f"{item['name']}: {d['title']}", url=d["url"],
                                author=first.get("author"), excerpt=(first.get("content") or "")[:400])
            else:
                old_ids = {p.get("id") for p in old.get("posts", [])}
                for p in disc["posts"]:
                    if p.get("id") not in old_ids:
                        self.change("new_post", f"{item['name']}: {d['title']}", url=d["url"], author=p.get("author"),
                                    excerpt=(p.get("content") or "")[:400])
            out.append(disc)
        return out

    def grades(self) -> list[dict[str, Any]]:
        try:
            html = self.m.html(f"/grade/report/user/index.php?id={self.cid}")
        except AuthExpired:
            raise
        except Exception as exc:
            self.errors.append(f"grades: {exc}")
            return []
        soup = BeautifulSoup(html, "lxml")
        for el in soup.select("table.user-grade .action-menu, table.user-grade .dropdown, table.user-grade .actions, table.user-grade img"):
            el.decompose()
        rows = []
        for tr in soup.select("table.user-grade tr"):
            name = tr.select_one("th.column-itemname")
            grade = tr.select_one("td.column-grade")
            if not name or not grade or "category" in (name.get("class") or []):
                continue
            title = name.select_one(".rowtitle, a.gradeitemheader") or name
            clean = lambda t: re.sub(r"\s*(Actions|Grade analysis)\s*", " ", t).strip()  # noqa: E731
            get = lambda c: (clean(tr.select_one(c).get_text(" ", strip=True)) if tr.select_one(c) else "")  # noqa: E731
            rows.append({"item": title.get_text(" ", strip=True), "grade": clean(grade.get_text(" ", strip=True)),
                         "range": get(".column-range"), "percentage": get(".column-percentage"),
                         "feedback": get(".column-feedback"), "weight": get(".column-weight")})
        return rows

    # -- main ----------------------------------------------------------------
    def run(self) -> dict[str, Any]:
        self.log(f"[{self.code}] syncing {self.name}")
        state = self.m.course_state(self.cid)
        sections = state.get("section", [])
        cms = {str(c["id"]): c for c in state.get("cm", [])}
        prev_files: dict[str, Any] = {}
        for it in self.prev_items.values():
            for f in it.get("files") or []:
                prev_files[f["path"]] = f

        items: dict[str, Any] = {}
        announcements: list[dict[str, Any]] = []
        for sec in sections:
            for cmid in sec.get("cmlist", []):
                c = cms.get(str(cmid))
                if not c:
                    continue
                item = {"id": int(c["id"]), "name": re.sub(r"\s+", " ", c.get("name") or "").strip(), "module": c.get("module"),
                        "section": sec.get("number"), "section_title": sec.get("title"), "url": c.get("url"),
                        "available": bool(c.get("uservisible", True))}
                items[str(c["id"])] = item
                if not item["url"] or not item["available"]:
                    continue
                try:
                    mod = item["module"]
                    if mod == "resource":
                        self.do_resource(item, prev_files)
                    elif mod == "folder":
                        self.do_folder(item, prev_files)
                    elif mod == "assign":
                        self.do_assign(item, prev_files)
                    elif mod in PAGE_MODULES:
                        self.do_page(item, prev_files)
                    elif mod == "url":
                        self.do_url(item, prev_files)
                    elif mod == "forum":
                        announcements += self.do_forum(item)
                except AuthExpired as exc:
                    self.m.dashboard()  # raises AuthExpired again if the session really is dead
                    self.errors.append(f"{item['module']} '{item['name']}': login redirect on this item only ({exc})")
                    item["error"] = str(exc)
                except Exception as exc:
                    self.errors.append(f"{item['module']} '{item['name']}': {exc}")
                    item["error"] = str(exc)

        # section pages -> readable course page
        section_md: list[tuple[dict, str]] = []
        prev_sections = self.prev.get("sections", {})
        sec_hashes = {}
        for sec in sections:
            if not sec.get("cmlist") and not sec.get("hassummary"):
                continue
            try:
                md = page_md(self.m.html(sec["sectionurl"]))
            except AuthExpired:
                raise
            except Exception as exc:
                md = f"_(could not load section: {exc})_"
            md = strip_volatile(md)
            sec_hashes[str(sec["id"])] = {"hash": sha(md), "title": sec.get("title"), "text": md}
            old = prev_sections.get(str(sec["id"]))
            if old and old.get("hash") != sec_hashes[str(sec["id"])]["hash"]:
                added = [ln for ln in md.splitlines() if ln.strip() and ln not in set((old.get("text") or "").splitlines())]
                if added:
                    self.change("section_updated", f"{sec.get('title')}", added=" / ".join(added)[:600])
            section_md.append((sec, md))

        grades = self.grades()
        self.diff_items(items, grades)

        index = {"synced_at": now_iso(), "course": {"code": self.code, "id": self.cid, "name": self.name,
                 "url": f"{self.m.base_url}/course/view.php?id={self.cid}"},
                 "items": items, "announcements": announcements, "grades": grades, "sections": sec_hashes,
                 "errors": self.errors, "stats": self.stats}
        write_json(self.dir / "index.json", index)
        write_json(self.dir / "announcements.json", announcements)
        write_json(self.dir / "grades.json", grades)
        (self.dir / "COURSE.md").write_text(render_course_md(index, section_md))
        (self.dir / "ANNOUNCEMENTS.md").write_text(render_announcements_md(self.name, announcements))
        (self.dir / "GRADES.md").write_text(render_grades_md(self.name, grades))
        if not self.prev:
            self.changes = [{"at": now_iso(), "course": self.code, "kind": "baseline",
                             "title": f"first v2 sync: {len(items)} activities, {self.stats['downloaded']} files"}]
        log_changes(self.changes)
        for old in ("course.json", "due-soon.json", "fetched-resources.json", "manifest.json"):
            (self.dir / old).unlink(missing_ok=True)
        self.log(f"[{self.code}] done: {len(items)} activities, {self.stats['downloaded']} downloaded, "
                 f"{self.stats['unchanged']} unchanged, {self.stats['skipped']} skipped, {len(self.changes)} changes, "
                 f"{len(self.errors)} errors")
        return {"code": self.code, "changes": self.changes, "errors": self.errors, "stats": self.stats}

    def diff_items(self, items: dict[str, Any], grades: list[dict[str, Any]]) -> None:
        if not self.prev:
            return
        for k, it in items.items():
            old = self.prev_items.get(k)
            if not old:
                self.change("new_activity", f"{it['module']}: {it['name']}", section=it.get("section_title"), url=it.get("url"))
                continue
            if it["module"] == "assign":
                for field in ("Submission status", "Grading status", "Group"):
                    a, b = (old.get("status") or {}).get(field), (it.get("status") or {}).get(field)
                    if a != b and b is not None:
                        self.change("assignment_status", f"{it['name']}: {field}", before=a, after=b, url=it.get("url"))
            for field in ("Due", "Closes", "Cut-off date"):
                a, b = (old.get("dates") or {}).get(field), (it.get("dates") or {}).get(field)
                if a and b and a != b:
                    self.change("date_changed", f"{it['name']}: {field}", before=a, after=b, url=it.get("url"))
            if not old.get("available") and it.get("available"):
                self.change("now_available", f"{it['module']}: {it['name']}", url=it.get("url"))
        for k, old in self.prev_items.items():
            if k not in items:
                self.change("removed_activity", f"{old['module']}: {old['name']}")
        old_grades = {g["item"]: g for g in self.prev.get("grades") or []}
        for g in grades:
            o = old_grades.get(g["item"])
            if g["grade"] not in ("", "-") and (not o or o.get("grade") != g["grade"]):
                self.change("grade", f"{g['item']}: {g['grade']}", feedback=g.get("feedback", "")[:300])


VOLATILE = [re.compile(p) for p in (r"\d+ days? \d+ hours? remaining", r"Assignment is overdue by:[^\n]*", r"\d+ (hours?|mins?|secs?) ago")]


def strip_volatile(md: str) -> str:
    for p in VOLATILE:
        md = p.sub("", md)
    return md


def log_changes(changes: list[dict[str, Any]]) -> None:
    if not changes:
        return
    with open(meta_dir() / "changes.jsonl", "a") as fh:
        for c in changes:
            fh.write(json.dumps(c, ensure_ascii=False) + "\n")


def fmt_dt(value: str | None) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).astimezone(UK).strftime("%a %d %b %Y %H:%M")
    except ValueError:
        return value


# -- markdown renderers ------------------------------------------------------
def render_course_md(index: dict[str, Any], section_md: list[tuple[dict, str]]) -> str:
    c = index["course"]
    items = index["items"]
    out = [f"# {c['name']}", "", f"Moodle: {c['url']}  ", f"Synced: {fmt_dt(index['synced_at'])} UK. Files in `files/`, text in `text/`, "
           "assignment/quiz/page details in `pages/`, announcements in `ANNOUNCEMENTS.md`, grades in `GRADES.md`.", ""]
    assigns = [i for i in items.values() if i["module"] in ("assign", "quiz") and i.get("available")]
    if assigns:
        out += ["## Assessments", "", "| Item | Due/closes (UK) | Status |", "|---|---|---|"]
        for a in sorted(assigns, key=lambda i: (i.get("dates") or {}).get("Due") or (i.get("dates") or {}).get("Closes") or "9"):
            d = a.get("dates") or {}
            due = d.get("Due") or d.get("Closes") or d.get("Closed") or ""
            st = (a.get("status") or {}).get("Submission status", "")
            out.append(f"| [{a['name']}]({a.get('page', '')}) | {fmt_dt(due)} | {st} |")
        out.append("")
    if index.get("errors"):
        out += ["## Sync problems", ""] + [f"- {e}" for e in index["errors"][:20]] + [""]
    out += ["## Course content", ""]
    by_section: dict[Any, list[dict]] = {}
    for it in items.values():
        by_section.setdefault(it["section"], []).append(it)
    for sec, md in section_md:
        out += [f"### {sec.get('number')}. {sec.get('title')}", "", md.strip(), ""]
        local = []
        for it in by_section.get(sec.get("number"), []):
            for f in it.get("files") or []:
                if f.get("skipped") or f.get("error"):
                    local.append(f"- {it['name']}: {f.get('skipped') or f.get('error')} — {f.get('url')}")
                else:
                    local.append(f"- `{f['path']}`" + (f" → `{f['text']}`" if f.get("text") else ""))
            if it.get("page") and it["module"] != "assign":
                local.append(f"- `{it['page']}`")
            if it.get("target"):
                local.append(f"- {it['name']} → {it['target']}")
            if it["module"] == "lti":
                local.append(f"- {it['name']} (external tool, e.g. recordings) → {it.get('url')}")
            if not it.get("available"):
                local.append(f"- {it['name']}: not available yet (restricted)")
        if local:
            out += ["**Local copies / links:**", ""] + local + [""]
    return "\n".join(out) + "\n"


def render_announcements_md(name: str, discussions: list[dict[str, Any]]) -> str:
    out = [f"# Announcements & forums — {name}", ""]
    for d in discussions:
        out += [f"## {d['title']}", f"_{d['forum']}_ — {d['url']}", ""]
        for p in d.get("posts") or []:
            when = fmt_dt(p.get("created_at")) if p.get("created_at") else (p.get("created_label") or "")
            out += [f"**{p.get('author') or '?'}**, {when}:", "", (p.get("content") or "").strip(), ""]
    if not discussions:
        out.append("_No forum posts._")
    return "\n".join(out) + "\n"


def render_grades_md(name: str, grades: list[dict[str, Any]]) -> str:
    out = [f"# Grades — {name}", "", "| Item | Grade | Range | % | Feedback |", "|---|---|---|---|---|"]
    for g in grades:
        out.append(f"| {g['item']} | {g['grade']} | {g['range']} | {g['percentage']} | {g['feedback'][:120]} |")
    return "\n".join(out) + "\n"
