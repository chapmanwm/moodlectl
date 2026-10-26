"""moodlectl — unofficial CLI for University of Glasgow Moodle. Mirrors your courses into plain local files.

Commands (all default to every tracked course):
  status                      auth + tracked courses + last sync
  courses [--all]             list enrolled courses (code, id, tracked?)
  track CODE|ID ... / untrack CODE ...
  sync [COURSE ...]           mirror courses into the corpus (incremental); prints what changed
  changes [--since 24h] [COURSE ...]
  todo [--days 60] [--all]    assessments with due dates + submission status, soonest first
  news [COURSE ...] [--since 7d]   announcements / forum posts
  grades [COURSE ...]
  find REGEX [COURSE ...]     search synced text (slides, briefs, pages, announcements)
  assignment ID|URL           live assignment page: dates, status, brief
  get URL [--out DIR]         download any Moodle file/resource URL
  auth set-cookie|import-cookie-file|status|clear   manage the MoodleSession cookie
  legacy ...                  the old v1 CLI (dashboard, due-soon, course, calendar-export, ...)
Exit codes: 0 ok, 1 error, 2 Moodle session expired (refresh the cookie with `moodlectl auth set-cookie`).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import sync as S
from .auth import extract_cookie_value
from .web import DEFAULT_BASE_URL, AuthExpired, Moodle, MoodleError, config_path, filename_from_url, load_config, save_config

# -- tracked courses ---------------------------------------------------------


def courses_file() -> Path:
    return S.meta_dir() / "courses.json"


def load_tracked() -> dict[str, Any]:
    return S.read_json(courses_file(), {}) or {}


def save_tracked(data: dict[str, Any]) -> None:
    S.write_json(courses_file(), dict(sorted(data.items())))


def ensure_tracked(m: Moodle) -> dict[str, Any]:
    tracked = load_tracked()
    if tracked:
        return tracked
    courses = m.courses("inprogress")
    tags = Counter(t for c in courses for t in re.findall(r"\b(\d\d/\d\d)\b", c.get("fullname", "")) if S.course_code(c))
    current = max(tags) if tags else None
    for c in courses:
        code = S.course_code(c)
        if code and (current is None or current in c.get("fullname", "")):
            tracked[code] = {"id": int(c["id"]), "name": c.get("fullname")}
    save_tracked(tracked)
    return tracked


def resolve(m: Moodle | None, names: list[str]) -> dict[str, Any]:
    tracked = load_tracked() if m is None else ensure_tracked(m)
    if not names:
        return tracked
    out = {}
    for n in names:
        key = n.upper()
        hit = [(k, v) for k, v in tracked.items() if k == key or str(v["id"]) == n or key in (v.get("name") or "").upper()]
        if not hit:
            raise MoodleError(f"'{n}' is not a tracked course. Tracked: {', '.join(tracked)}. Use `moodlectl track {n}`.")
        out.update(hit[:1])
    return out


def require_synced() -> None:
    """Offline commands read the local mirror; fail loudly instead of reporting 'nothing' before the first sync."""
    if not any((S.corpus_root() / code / "moodle" / "index.json").exists() for code in load_tracked()):
        raise MoodleError(f"Nothing synced yet in {S.corpus_root()}. Run `moodlectl sync` first (it picks up this year's courses automatically).")


# -- output helpers ----------------------------------------------------------


def emit(args: argparse.Namespace, data: Any, text: str) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False) if args.json else text)


def parse_since(value: str) -> datetime:
    m = re.fullmatch(r"(\d+)\s*([hdw])", value.strip())
    now = datetime.now(timezone.utc)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return now - {"h": timedelta(hours=n), "d": timedelta(days=n), "w": timedelta(weeks=n)}[unit]
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=S.UK)


# -- commands ----------------------------------------------------------------


def cmd_status(args) -> int:
    info: dict[str, Any] = {"corpus": str(S.corpus_root())}
    try:
        m = Moodle()
        m.dashboard()
        info["moodle"] = "ok"
    except AuthExpired as exc:
        info["moodle"] = f"EXPIRED: {exc}"
    # Optional: a login helper can publish its own state (JSON) for agents to see here.
    helper_status_file = os.environ.get("MOODLECTL_AUTH_STATUS_FILE")
    helper_status = S.read_json(Path(helper_status_file)) if helper_status_file else None
    if helper_status:
        info["auth_helper"] = helper_status
    rows = []
    for code, c in load_tracked().items():
        idx = S.read_json(S.corpus_root() / code / "moodle" / "index.json", {}) or {}
        rows.append({"code": code, "id": c["id"], "last_sync": idx.get("synced_at"), "errors": len(idx.get("errors") or [])})
    info["courses"] = rows
    lines = [f"Moodle session: {info['moodle']}", f"Corpus: {info['corpus']}"]
    lines += [f"  {r['code']:8} {r['id']:>6}  last sync {S.fmt_dt(r['last_sync']) or 'never'}" + (f"  ({r['errors']} errors)" if r["errors"] else "") for r in rows]
    emit(args, info, "\n".join(lines))
    return 0 if info["moodle"] == "ok" else 2


def cmd_courses(args) -> int:
    m = Moodle()
    tracked = ensure_tracked(m)
    ids = {v["id"] for v in tracked.values()}
    courses = m.courses("all" if args.all else "inprogress")
    rows = [{"code": S.course_code(c), "id": int(c["id"]), "name": c.get("fullname"), "tracked": int(c["id"]) in ids} for c in courses]
    emit(args, rows, "\n".join(f"{'*' if r['tracked'] else ' '} {str(r['code'] or '-'):8} {r['id']:>6}  {r['name']}" for r in rows)
         + "\n(* = tracked; `moodlectl track <code|id>` to add)")
    return 0


def cmd_track(args) -> int:
    m = Moodle()
    tracked = ensure_tracked(m)
    allc = m.courses("all")
    for n in args.courses:
        hit = [c for c in allc if str(c["id"]) == n or (S.course_code(c) or "").upper() == n.upper()]
        if not hit:
            print(f"no enrolled course matches {n}", file=sys.stderr)
            return 1
        c = hit[0]
        tracked[S.course_code(c) or str(c["id"])] = {"id": int(c["id"]), "name": c.get("fullname")}
        print(f"tracking {S.course_code(c)} {c.get('fullname')}")
    save_tracked(tracked)
    return 0


def cmd_untrack(args) -> int:
    tracked = load_tracked()
    for n in args.courses:
        tracked.pop(n.upper(), None)
    save_tracked(tracked)
    print("tracked:", ", ".join(tracked))
    return 0


def cmd_sync(args) -> int:
    m = Moodle()
    targets = resolve(m, args.courses)
    log = (lambda s: None) if args.json else (lambda s: print(s, flush=True))
    results = []
    for code, c in targets.items():
        try:
            results.append(S.CourseSync(m, code, c["id"], c["name"], videos=args.videos, max_mb=args.max_mb, log=log).run())
        except AuthExpired:
            raise
        except Exception as exc:
            results.append({"code": code, "changes": [], "errors": [f"sync failed: {exc}"]})
            log(f"[{code}] FAILED: {exc}")
    S.write_json(S.meta_dir() / "last-sync.json", {"at": S.now_iso(), "results": results})
    changes = [c for r in results for c in r["changes"] if c["kind"] != "baseline"]
    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        print()
        print(format_changes(changes) if changes else "No changes since last sync.")
        errs = [f"{r['code']}: {e}" for r in results for e in r["errors"]]
        if errs:
            print(f"\n{len(errs)} problems (see COURSE.md 'Sync problems'):")
            print("\n".join(f"  {e}" for e in errs[:15]))
    return 0


KIND_LABEL = {"new_file": "New file", "updated_file": "Updated file", "new_activity": "New activity", "removed_activity": "Removed",
              "new_announcement": "Announcement", "new_post": "Forum reply", "assignment_status": "Status", "date_changed": "Date changed",
              "grade": "Grade", "section_updated": "Page text changed", "page_updated": "Page updated", "now_available": "Now available"}


def format_changes(changes: list[dict[str, Any]]) -> str:
    out = []
    for code in sorted({c["course"] for c in changes}):
        out.append(f"{code}:")
        for c in [x for x in changes if x["course"] == code]:
            line = f"  - {KIND_LABEL.get(c['kind'], c['kind'])}: {c['title']}"
            if c.get("before") is not None or c.get("after") is not None:
                line += f" ({c.get('before')} → {c.get('after')})"
            if c.get("excerpt"):
                line += f"\n      “{c['excerpt'][:240].strip()}”"
            if c.get("added"):
                line += f"\n      + {c['added'][:240]}"
            out.append(line)
    return "\n".join(out)


def cmd_changes(args) -> int:
    require_synced()
    since = parse_since(args.since)
    codes = set(resolve(None, args.courses)) if args.courses else None
    rows = []
    path = S.meta_dir() / "changes.jsonl"
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                c = json.loads(line)
            except json.JSONDecodeError:
                continue
            if c["kind"] == "baseline" or datetime.fromisoformat(c["at"]) < since:
                continue
            if codes and c["course"] not in codes:
                continue
            rows.append(c)
    emit(args, rows, format_changes(rows) if rows else f"No changes since {S.fmt_dt(since.isoformat())}. (Run `moodlectl sync` first for fresh data.)")
    return 0


def collect_todo(days: int, include_all: bool) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    rows = []
    for code in load_tracked():
        idx = S.read_json(S.corpus_root() / code / "moodle" / "index.json", {}) or {}
        for it in (idx.get("items") or {}).values():
            if it["module"] not in ("assign", "quiz"):
                continue
            d = it.get("dates") or {}
            due_s = d.get("Due") or d.get("Closes") or d.get("Closed") or d.get("Cut-off date")
            due = None
            try:
                due = datetime.fromisoformat(due_s) if due_s else None
            except ValueError:
                pass
            st = it.get("status") or {}
            sub = st.get("Submission status", "")
            done = bool(re.search(r"submitted for grading|^submitted", sub, re.I))
            if not include_all:
                if due is None or due > now + timedelta(days=days):
                    continue
                if due < now - timedelta(days=30) and done:
                    continue
            flag = "done" if done else ("OVERDUE" if due and due < now else "todo")
            if re.search(r"dummy|grades will be here|no need to submit", it["name"] + " " + sub, re.I) and not done:
                flag = "no-submission"
            rows.append({"course": code, "name": it["name"], "due": due.isoformat() if due else None, "flag": flag,
                         "submission": sub, "grading": st.get("Grading status", ""), "group": st.get("Group"),
                         "page": str(S.corpus_root() / code / "moodle" / it["page"]) if it.get("page") else None, "url": it.get("url")})
    rows.sort(key=lambda r: r["due"] or "9999")
    return rows


def cmd_todo(args) -> int:
    require_synced()
    rows = collect_todo(args.days, args.all)
    S.write_json(S.meta_dir() / "todo.json", rows)
    lines = []
    for r in rows:
        due = S.fmt_dt(r["due"]) if r["due"] else "no date"
        extra = f" [{r['group']}]" if r.get("group") and "not a member" in str(r["group"]).lower() else ""
        lines.append(f"{r['flag']:13} {due:22} {r['course']:8} {r['name']} — {r['submission'] or '-'}{extra}")
    emit(args, rows, "\n".join(lines) if lines else f"Nothing due in the next {args.days} days (from last sync).")
    return 0


def cmd_news(args) -> int:
    require_synced()
    since = parse_since(args.since)
    out, rows = [], []
    for code in resolve(None, args.courses):
        for d in S.read_json(S.corpus_root() / code / "moodle" / "announcements.json", []) or []:
            for p in d.get("posts") or []:
                try:
                    when = datetime.fromisoformat(p["created_at"]) if p.get("created_at") else None
                except ValueError:
                    when = None
                if when and when < since:
                    continue
                rows.append({"course": code, "forum": d["forum"], "title": d["title"], "author": p.get("author"),
                             "at": p.get("created_at"), "content": p.get("content"), "url": d["url"]})
    rows.sort(key=lambda r: r["at"] or "", reverse=True)
    for r in rows:
        out.append(f"## {r['course']} — {r['title']} ({r['author']}, {S.fmt_dt(r['at'])})\n{(r['content'] or '').strip()[:1500]}\n")
    emit(args, rows, "\n".join(out) if out else "No announcements in that window (from last sync).")
    return 0


def cmd_grades(args) -> int:
    require_synced()
    allrows = []
    for code in resolve(None, args.courses):
        for g in S.read_json(S.corpus_root() / code / "moodle" / "grades.json", []) or []:
            allrows.append({"course": code, **g})
    graded = [g for g in allrows if g["grade"] not in ("", "-")] if not args.all else allrows
    emit(args, graded, "\n".join(f"{g['course']:8} {g['item'][:60]:60} {g['grade']:>10} {g['percentage']}" for g in graded)
         or "No grades released yet (from last sync). Use --all to list every grade item.")
    return 0


def cmd_find(args) -> int:
    require_synced()
    roots = [S.corpus_root() / code / "moodle" for code in resolve(None, args.courses)]
    roots = [r for r in roots if r.exists()]
    if shutil.which("rg"):
        cmd = ["rg", "-i", "-n", "--max-count", "3", "--max-columns", "220", "-g", "!files/**", "-g", "!*.json", args.pattern, *map(str, roots)]
        res = subprocess.run(cmd, capture_output=True, text=True)
        print(res.stdout.strip() or "no matches")
        return 0
    rx = re.compile(args.pattern, re.I)
    for r in roots:
        for f in r.rglob("*"):
            if f.is_file() and f.suffix in (".txt", ".md"):
                for i, line in enumerate(f.read_text(errors="ignore").splitlines(), 1):
                    if rx.search(line):
                        print(f"{f}:{i}:{line[:220]}")
    return 0


def cmd_assignment(args) -> int:
    from .parsers import parse_assignment_html

    m = Moodle()
    target = args.target
    url = target if target.startswith("http") else f"{m.base_url}/mod/assign/view.php?id={int(target)}"
    html = m.html(url)
    data = parse_assignment_html(m.base_url, url, html)
    data["dates"] = S.activity_dates(html)
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0
    print(f"# {data.get('name')}\n{url}\n")
    for k, v in data["dates"].items():
        print(f"{k}: {S.fmt_dt(v)}")
    for k, v in (data.get("status") or {}).items():
        print(f"{k}: {v}")
    print("\n" + (data.get("description") or "").strip())
    for link in data.get("links") or []:
        if "pluginfile" in link["url"]:
            print(f"- attachment: {link['text']} -> {link['url']}")
    return 0


def cmd_get(args) -> int:
    m = Moodle()
    url = m.url(args.url)
    if "/mod/resource/" in url:
        url = m.head_location(url) or url
    out_dir = Path(args.out or (S.corpus_root() / "_downloads"))
    dest = out_dir / filename_from_url(url)
    res = m.download(url, dest)
    print(f"saved {dest} ({res.get('size', 0) // 1000} kB)" if not res.get("skipped") else f"skipped: {res['skipped']}")
    return 0


def _save_cookie(value: str, base_url: str | None) -> None:
    cfg = load_config()
    cfg["moodle_session"] = value
    cfg["base_url"] = (base_url or cfg.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    save_config(cfg)
    print(f"Saved cookie for {cfg['base_url']} to {config_path()} (mode 600).", file=sys.stderr)


def cmd_auth(args) -> int:
    if args.auth_cmd == "set-cookie":
        if args.cookie and not args.stdin:
            raw = args.cookie
        elif args.stdin or not sys.stdin.isatty():
            raw = sys.stdin.read()
        else:
            import getpass

            raw = getpass.getpass("Paste MoodleSession cookie (input hidden): ")
        _save_cookie(extract_cookie_value(raw), args.base_url)
        return cmd_auth_status(args, verify=True)
    if args.auth_cmd == "import-cookie-file":
        _save_cookie(extract_cookie_value(Path(args.path).expanduser().read_text()), args.base_url)
        return cmd_auth_status(args, verify=True)
    if args.auth_cmd == "clear":
        p = config_path()
        cfg = load_config()
        cfg.pop("moodle_session", None)
        save_config(cfg) if cfg else p.unlink(missing_ok=True)
        print(f"Removed stored cookie from {p}.")
        return 0
    return cmd_auth_status(args, verify=not args.offline)


def cmd_auth_status(args, verify: bool = True) -> int:
    cfg = load_config()
    source = "env MOODLE_SESSION" if os.environ.get("MOODLE_SESSION") else ("config" if cfg.get("moodle_session") else None)
    info: dict[str, Any] = {
        "config_path": str(config_path()),
        "base_url": (os.environ.get("MOODLE_BASE_URL") or cfg.get("base_url") or DEFAULT_BASE_URL).rstrip("/"),
        "cookie_source": source,
    }
    rc = 0 if source else 2
    if source and verify:
        try:
            Moodle().dashboard()
            info["session"] = "ok"
        except AuthExpired as exc:
            info["session"] = f"expired: {exc}"
            rc = 2
    lines = [f"Config: {info['config_path']}", f"Moodle: {info['base_url']}", f"Cookie: {source or 'not configured'}"]
    if "session" in info:
        lines.append(f"Session: {info['session']}")
    emit(args, info, "\n".join(lines))
    return rc


def cmd_legacy(args) -> int:
    from .legacy_cli import main as legacy_main

    return legacy_main(args.rest) or 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="moodlectl", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    s = sub.add_parser("courses"); s.add_argument("--all", action="store_true"); s.set_defaults(fn=cmd_courses)
    s = sub.add_parser("track"); s.add_argument("courses", nargs="+"); s.set_defaults(fn=cmd_track)
    s = sub.add_parser("untrack"); s.add_argument("courses", nargs="+"); s.set_defaults(fn=cmd_untrack)
    s = sub.add_parser("sync"); s.add_argument("courses", nargs="*"); s.add_argument("--videos", action="store_true", help="also download video/audio")
    s.add_argument("--max-mb", type=int, default=150); s.set_defaults(fn=cmd_sync)
    s = sub.add_parser("changes"); s.add_argument("courses", nargs="*"); s.add_argument("--since", default="24h"); s.set_defaults(fn=cmd_changes)
    s = sub.add_parser("todo"); s.add_argument("--days", type=int, default=60); s.add_argument("--all", action="store_true"); s.set_defaults(fn=cmd_todo)
    s = sub.add_parser("news"); s.add_argument("courses", nargs="*"); s.add_argument("--since", default="7d"); s.set_defaults(fn=cmd_news)
    s = sub.add_parser("grades"); s.add_argument("courses", nargs="*"); s.add_argument("--all", action="store_true"); s.set_defaults(fn=cmd_grades)
    s = sub.add_parser("find"); s.add_argument("pattern"); s.add_argument("courses", nargs="*"); s.set_defaults(fn=cmd_find)
    s = sub.add_parser("assignment"); s.add_argument("target"); s.set_defaults(fn=cmd_assignment)
    s = sub.add_parser("get"); s.add_argument("url"); s.add_argument("--out"); s.set_defaults(fn=cmd_get)
    a = sub.add_parser("auth", help="manage the MoodleSession cookie").add_subparsers(dest="auth_cmd", required=True)
    s = a.add_parser("set-cookie", help="save a cookie (argument, --stdin, or hidden prompt)")
    s.add_argument("cookie", nargs="?", help="cookie value; prefer --stdin to keep it out of shell history")
    s.add_argument("--stdin", action="store_true"); s.add_argument("--base-url")
    s = a.add_parser("import-cookie-file", help="read MoodleSession from cookies.txt / browser JSON export / Cookie header")
    s.add_argument("path"); s.add_argument("--base-url")
    s = a.add_parser("status", help="show where the cookie comes from and check it against Moodle")
    s.add_argument("--offline", action="store_true", help="don't contact Moodle")
    s.add_argument("--verify", action="store_true", help=argparse.SUPPRESS)  # v1 compat; verification is the default
    a.add_parser("clear", help="forget the stored cookie")
    sub.choices["auth"].set_defaults(fn=cmd_auth)
    s = sub.add_parser("legacy"); s.add_argument("rest", nargs=argparse.REMAINDER); s.set_defaults(fn=cmd_legacy)
    return p


LEGACY_COMMANDS = {"dashboard", "due-soon", "course", "timeline", "calendar-export", "announcements", "export", "fetch", "resource"}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # old v1 commands keep working transparently
    if argv and argv[0] in LEGACY_COMMANDS:
        from .legacy_cli import main as legacy_main

        return legacy_main(argv) or 0
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except AuthExpired as exc:
        print(f"AUTH_EXPIRED: {exc}", file=sys.stderr)
        return 2
    except MoodleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
