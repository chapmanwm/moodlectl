# moodlectl

**An unofficial command-line tool for University of Glasgow Moodle (`moodle.gla.ac.uk`).**

moodlectl mirrors your Moodle courses into plain local files: slides, briefs, pages, announcements, deadlines and grades. You can then grep, diff and read them offline. An AI agent can use them the same way. It's built for UofG Moodle, including its course formats and its Microsoft SSO redirects. It may partly work on other Moodle 4.x sites if you point `MOODLE_BASE_URL` at them, but nothing outside UofG is tested.

> Not affiliated with or endorsed by the University of Glasgow. It reads only what your own account can already see in a browser. You're responsible for using it in line with the University's IT regulations. Don't share your session cookie, and don't run it against accounts that aren't yours.

## What it does

moodlectl scrapes the same pages you see in a browser, because most of Moodle's web-service API is disabled at UofG. It handles:

- course pages in every format, including **tiles** and other per-section formats
- **folders** (every file inside, kept in a subdirectory)
- resources, assignment brief attachments, pages, books, quizzes, labels and URLs
- announcements and forums (full post text)
- grades, plus assignment dates and submission status

Each course is saved to `<corpus>/<CODE>/moodle/`:

```
COURSE.md            assessments table + every section's text + links to local files
ANNOUNCEMENTS.md     forum posts, newest first
GRADES.md
files/               downloads (folders → files/<Folder>/…, assignment briefs → files/<Assignment>/…)
text/                extracted text for every file (pdf, pptx, docx, xlsx, legacy office)
pages/               assignment / quiz / page details as markdown
index.json           machine-readable state used for diffing
```

Syncs are incremental. Unchanged files aren't downloaded again. Every run appends what changed (new files, new announcements, date changes, submission-status changes, released grades) to `<corpus>/_moodle/changes.jsonl`. Video and audio are skipped unless you pass `--videos`.

## Install

Requires Python 3.10+.

```bash
pip install 'moodlectl[office] @ git+https://github.com/chapmanwm/moodlectl.git'
# or: pipx install 'git+https://github.com/chapmanwm/moodlectl.git'
```

The `[office]` extra gives text extraction from PDF, PPTX, DOCX and XLSX in pure Python. If `pdftotext` (poppler: `apt install poppler-utils` / `brew install poppler`) or `pandoc` are installed, moodlectl uses them instead, which gives better layout and cleaner markdown.

## Log in

UofG Moodle uses Microsoft SSO with MFA, so moodlectl doesn't log in by itself. You log in through your browser and give moodlectl the session cookie:

1. Log in at <https://moodle.gla.ac.uk> in your browser.
2. Open DevTools. On Chrome/Edge it's under **Application → Cookies**, on Firefox under **Storage → Cookies**. Select `https://moodle.gla.ac.uk` and copy the value of `MoodleSession`.
3. Save it. The input is hidden, and nothing ends up in your shell history:

```bash
moodlectl auth set-cookie            # paste at the prompt
# or: pbpaste | moodlectl auth set-cookie --stdin
# or: moodlectl auth import-cookie-file ~/Downloads/cookies.txt   (cookies.txt / browser JSON export)
moodlectl auth status                # checks the cookie against Moodle
```

The cookie is stored at `~/.config/moodlectl/config.json` with mode 600 and is never printed. It lasts about as long as your browser session. When it expires, every command exits with code **2** and you repeat step 2. `MOODLE_SESSION=... moodlectl ...` also works, for secret stores and CI. See [docs/authentication.md](docs/authentication.md).

## Use

```bash
moodlectl courses                # enrolled courses; this year's are tracked automatically
moodlectl sync                   # mirror every tracked course (first run takes a few minutes)
moodlectl todo                   # what's due, soonest first, with submission status
moodlectl changes --since 24h    # what changed since yesterday
moodlectl news --since 7d        # recent announcements
moodlectl find 'lab report'      # search the synced text
```

| Command | What it does |
|---|---|
| `status` | session check + tracked courses + last sync |
| `courses [--all]` | enrolled courses (code, id, tracked?) |
| `track CODE\|ID …` / `untrack CODE …` | choose which courses `sync` mirrors |
| `sync [CODE …] [--videos] [--max-mb N]` | incremental mirror (all tracked courses by default) |
| `todo [--days 60] [--all]` | assessments due/overdue with submission status |
| `changes [--since 24h] [CODE …]` | change log |
| `news [CODE …] [--since 7d]` | announcements and forum posts |
| `grades [CODE …] [--all]` | released grades (from the last sync) |
| `find REGEX [CODE …]` | search synced slides, briefs, pages, announcements |
| `assignment <cmid\|url>` | live assignment page: dates, status, brief |
| `get <url> [--out DIR]` | download any Moodle file/resource URL |
| `auth set-cookie\|import-cookie-file\|status\|clear` | manage the session cookie |

Add `--json` before the command (`moodlectl --json todo`) for machine-readable output. Exit codes: `0` ok, `1` error, `2` session expired.

The original v1 commands still work (`dashboard`, `due-soon`, `course`, `timeline`, `calendar-export`, `announcements`, `export`, `fetch`, `resource`). You can also reach them as `moodlectl legacy …`.

## Using it from an AI agent

moodlectl was designed so a coding/assistant agent can read your coursework. Have the agent run `moodlectl sync` (or schedule it), then read the markdown files in the corpus instead of hitting Moodle on every question. A ready-made skill file for agents that support `SKILL.md` (Claude Code, Codex and others) is in [`skills/moodlectl/`](skills/moodlectl/SKILL.md). See also [docs/agent-usage.md](docs/agent-usage.md).

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `MOODLE_SESSION` | — | cookie value (overrides the stored one) |
| `MOODLE_BASE_URL` | `https://moodle.gla.ac.uk` | Moodle site |
| `MOODLECTL_CONFIG_DIR` | `~/.config/moodlectl` | where `config.json` lives |
| `MOODLECTL_CORPUS` | `~/.local/share/moodlectl/corpus` | where courses are mirrored |
| `MOODLECTL_REAUTH_HINT` | generic | text added to the session-expired error (e.g. how *your* setup refreshes the cookie) |
| `MOODLECTL_AUTH_STATUS_FILE` | — | optional JSON written by your own login helper; shown in `moodlectl status` |
| `MOODLECTL_CACHE_DIR` | `~/.local/share/moodlectl` | cache for the v1 commands |

## Notes and limits

- Courses roll over each year and often keep last year's dates in section titles and slides. `todo` and `pages/` read the real dates from the assignment pages.
- Course codes are detected from names like `ENG1234`. Courses without a code aren't auto-tracked; use `track <id>`.
- Times are shown in Europe/London.
- A full first sync of ~7 courses takes several minutes and makes a lot of requests. Later syncs are much lighter. Please don't run it on a tight loop.

## Development

```bash
git clone https://github.com/chapmanwm/moodlectl.git && cd moodlectl
python -m venv .venv && . .venv/bin/activate
pip install -e '.[office,dev]'
python -m pytest -q
```

The HTML fixtures in `tests/fixtures/` are trimmed captures of real UofG Moodle pages. All user, course and ID data in them has been replaced with placeholders. Keep it that way: never commit real cookies, sesskeys, names or student IDs.

## License

[MIT](LICENSE)
