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
pip install 'moodlectl[office,login] @ git+https://github.com/chapmanwm/moodlectl.git'
# or: pipx install 'moodlectl[office,login] @ git+https://github.com/chapmanwm/moodlectl.git'
```

- `office`: pure-Python text extraction (PDF, PPTX, DOCX, XLSX).
- `login`: the automated UofG SSO login (`moodlectl login`). It uses [Camoufox](https://camoufox.com), a hardened Firefox build. The first login downloads the browser once (about 1.3 GB). On a bare Linux server/container, also run `python -m playwright install-deps firefox` once for its system libraries. Leave it out if you'd rather paste a cookie yourself.

The `[office]` extra gives text extraction from PDF, PPTX, DOCX and XLSX in pure Python. If `pdftotext` (poppler: `apt install poppler-utils` / `brew install poppler`) or `pandoc` are installed, moodlectl uses them instead, which gives better layout and cleaner markdown.

## Log in

```bash
moodlectl login
```

This opens a headless browser and goes through UofG's Microsoft login for you. It asks for your UofG email and password if they aren't saved yet. When Microsoft wants MFA, it prints a two-digit number:

```
MFA_NUMBER 47
→ Open Microsoft Authenticator and enter 47.
```

Approve it on your phone and the session cookie is saved. moodlectl answers yes to "Stay signed in?" and keeps that browser profile. After that, `moodlectl login` usually finishes silently in ~30 s, with no password and no MFA, until Microsoft's own session expires.

To log in unattended (cron, agents, CI), save your credentials once (mode 600):

```bash
moodlectl auth set-credentials           # prompts; or: --email you@student.gla.ac.uk --password-stdin
moodlectl login --if-needed --no-mfa     # e.g. every few hours from cron; exits 3 if a human is needed for MFA
```

MFA safety rules (built in):
- **One push per run.** If a request is denied or expires, moodlectl stops (exit 4) and waits a 10-minute cooldown before allowing another run (`--force` skips it). It never resends: repeated pushes look like an MFA-fatigue attack, and Microsoft throttles the account.
- A wrong username or password stops immediately (exit 5), so it can't lock your account.
- Only one login can run at a time.

`login` exit codes: `0` ok · `1` setup problem · `2` timed out / cookie didn't verify · `3` MFA needed but `--no-mfa` · `4` MFA denied/expired/throttled · `5` wrong username/password · `6` cooldown or another login running. Add `--headed` to watch the browser, or `--debug` for screenshots.

**Prefer not to automate the login?** Log in at <https://moodle.gla.ac.uk> in your browser, copy the `MoodleSession` cookie from DevTools (Application/Storage → Cookies), and run `moodlectl auth set-cookie` (hidden prompt). You can also use `auth import-cookie-file cookies.txt`.

Where things are stored:
- **Session cookie:** `~/.config/moodlectl/config.json`, mode 600.
- **Credentials and the SSO browser profile:** `~/.config/moodlectl/` (directory mode 700). Set `MOODLECTL_SECRETS_DIR` to keep them somewhere an agent sandbox can't read.

Nothing secret is ever printed. When the session expires, every command exits with code **2**: run `moodlectl login` again. See [docs/authentication.md](docs/authentication.md).

## Use

```bash
moodlectl courses                # enrolled courses; this year's are tracked automatically
moodlectl sync                   # mirror every tracked course (first run takes a few minutes)
moodlectl todo                   # what's due, soonest first, with submission status
moodlectl changes --since 24h    # what changed since yesterday
moodlectl news --since 7d        # recent announcements
moodlectl find 'lab report'      # search the synced text
moodlectl timetable              # your classes for the next 7 days, in UK time
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
| `login [--if-needed] [--no-mfa] [--force] [--headed] [--debug]` | UofG SSO login in a headless browser (needs `[login]`) |
| `timetable [--days 7] [--offline]` | class timetable from the MyCampus/SPaG iCal feed (needs saved credentials) |
| `auth set-credentials\|forget-credentials` | save/remove your UofG email + password (mode 600) |
| `auth set-cookie\|import-cookie-file\|status\|clear` | manage the session cookie by hand |

Add `--json` before the command (`moodlectl --json todo`) for machine-readable output. Exit codes: `0` ok, `1` error, `2` session expired.

The original v1 commands still work (`dashboard`, `due-soon`, `course`, `timeline`, `calendar-export`, `announcements`, `export`, `fetch`, `resource`). You can also reach them as `moodlectl legacy …`.

## Using it from an AI agent

moodlectl was designed so a coding/assistant agent can read your coursework. Have the agent run `moodlectl sync` (or schedule it), then read the markdown files in the corpus instead of hitting Moodle on every question. A ready-made skill file for agents that support `SKILL.md` (Claude Code, Codex and others) is in [`skills/moodlectl/`](skills/moodlectl/SKILL.md). See also [docs/agent-usage.md](docs/agent-usage.md).

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `MOODLE_SESSION` | — | cookie value (overrides the stored one) |
| `MOODLE_USERNAME` / `MOODLE_PASSWORD` | — | UofG login for `login`/`timetable` (override saved credentials) |
| `UOFG_GUID` | part of the email before `@` | timetable feed username, if different |
| `MOODLECTL_SECRETS_DIR` | the config dir | credentials + SSO browser profile |
| `MOODLE_BASE_URL` | `https://moodle.gla.ac.uk` | Moodle site |
| `MOODLECTL_CONFIG_DIR` | `~/.config/moodlectl` | where `config.json` lives |
| `MOODLECTL_CORPUS` | `~/.local/share/moodlectl/corpus` | where courses are mirrored |
| `MOODLECTL_REAUTH_HINT` | generic | text added to the session-expired error (e.g. how *your* setup refreshes the cookie) |
| `MOODLECTL_AUTH_STATUS_FILE` | `<config>/login-status.json` | non-secret login state (MFA pending/number, last login) shown in `moodlectl status` |
| `MOODLECTL_CACHE_DIR` | `~/.local/share/moodlectl` | cache for the v1 commands |

## Notes and limits

- Courses roll over each year and often keep last year's dates in section titles and slides. `todo` and `pages/` read the real dates from the assignment pages.
- Course codes are detected from names like `ENG1234`. Courses without a code aren't auto-tracked; use `track <id>`.
- Times are shown in Europe/London.
- The timetable feed and the SSO login are UofG-specific. The Moodle sync may partly work elsewhere; login and timetable won't.
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
