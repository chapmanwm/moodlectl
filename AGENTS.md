# moodlectl contributor guide (humans and coding agents)

`moodlectl` is an unofficial CLI for **University of Glasgow** Moodle. It mirrors a student's courses into plain local files (markdown, original downloads, extracted text) that people and AI agents can read and grep. UofG-specific behaviour (SSO hosts, timetable feed, default URL) is intentional; don't genericise it away.

If you're an agent *using* moodlectl rather than changing it, read `skills/moodlectl/SKILL.md` and `docs/agent-usage.md` instead.

## Layout

| Module | Role |
|---|---|
| `main.py` | v2 CLI (argparse): command handlers `cmd_*`, `--json` output via `emit()`, exit-code mapping in `main()` |
| `web.py` | HTTP layer: one authenticated `requests` session, config dir/file, `AuthExpired` (any redirect to a login/SSO host), polite downloads |
| `auth.py` | `extract_cookie_value()`: parse a MoodleSession from a bare value, header, cookies.txt or JSON export |
| `login.py` | `moodlectl login`: Camoufox-driven UofG Microsoft SSO. `classify(Probe)` is a pure page→action state machine; only `_probe`/`browser_login` touch the browser |
| `timetable.py` | MyCampus/SPaG iCal feed: fetch (basic auth), parse, render in Europe/London |
| `sync.py` | course mirror: tracked courses, incremental downloads, `changes.jsonl`, text backfill |
| `render.py` | HTML→markdown and file→text (pandoc/pdftotext if present, else pypdf/python-docx from `[office]`) |
| `parsers.py` | deterministic HTML parsers (assignment pages, sesskey, text cleanup) |
| `client.py`, `legacy_cli.py` | v1 CLI, reachable only via `moodlectl legacy …`. Keep it working; don't add features there |

Optional extras: `[office]` (pure-Python text extraction), `[login]` (camoufox, pinned `<0.5`), `[dev]` (pytest). The core install must work without any extra: import camoufox, pypdf etc. lazily inside the function that needs them.

## Contract with callers

- **stdout = payload, stderr = progress/logging.** `--json` output must stay valid JSON on stdout.
- **Exit codes are API.** `0` ok, `1` error / not yet synced, `2` Moodle session expired (`AUTH_EXPIRED:` on stderr). `login` adds `3` MFA needed with `--no-mfa`, `4` MFA denied/expired/throttled, `5` bad credentials, `6` cooldown/another login running. Don't renumber; add new codes only with docs.
- Offline commands (`todo`, `news`, `changes`, `grades`, `find`) must fail with exit 1 before the first sync (`require_synced()`), never report "nothing due".
- Times shown to users are UK local time.
- Corpus layout (`<corpus>/<CODE>/moodle/{COURSE.md,pages/,files/,text/}`, `_moodle/`) is read by agents. Treat renames as breaking changes.

## Secrets and MFA (non-negotiable)

- Never print, log or write the MoodleSession cookie or the UofG password anywhere except their mode-600 files (`config.json`, `credentials.json`). Status files, `--json` output, debug output and exceptions must not contain them.
- Secrets dirs are created mode 700. `MOODLECTL_SECRETS_DIR` must keep working, so credentials and the SSO profile can live outside an agent sandbox.
- **One MFA push per `login` run.** Never add retry/resend loops around MFA. Denied/expired/throttled → stop with exit 4, record `mfa_failed_at`, honour the 10-minute cooldown unless `--force`. Repeated pushes look like MFA fatigue and get the account throttled.
- Stop immediately on wrong credentials (exit 5); never retry a password.
- Keep the single-run lock: two concurrent logins mean two pushes.
- Interactive prompts are allowed only when stdin is a TTY. Otherwise fail with a message pointing at `auth set-credentials` / env vars.

## Changing the SSO flow

Microsoft and UofG change their pages. When login breaks:

1. Reproduce with `moodlectl login --headed --debug` (screenshots + page text in `<config>/login-debug/`; delete them afterwards, they show your account).
2. Add the new page as a `Probe` case in `tests/test_login.py::ClassifyTests` first, then adjust `classify()`. Order of checks matters (e.g. the password page also contains the email input).
3. Only then touch selectors in `browser_login`. Known traps: use `press_sequentially` + Enter for the password (fill + click submitted empty passwords); Microsoft's footer overlay intercepts clicks on `#idSIButton9`; read `inner_text`, not `text_content`.

## Tests

```bash
pip install -e '.[office,dev]'
python -m pytest -q
```

- Tests never hit the network or launch a browser; mock `requests`, `browser_login` and `cookie_is_live`.
- Fixtures in `tests/fixtures/uofg_*` are real page structures with **anonymised** content (example course codes, no names, IDs or analytics). Anything new you add must be sanitised the same way, and the parser output must still match the original.
- CI runs Python 3.10–3.13, plus a job with pandoc/poppler installed.

## Release checklist

- [ ] Tests added/updated, `pytest -q` green locally; `pyflakes src tests` clean for touched files
- [ ] Core install without extras still imports and runs (`moodlectl -h`, `moodlectl login` gives the "needs [login]" message)
- [ ] stdout/stderr split and exit codes checked for new/changed commands
- [ ] README command table, `docs/` and `skills/moodlectl/SKILL.md` updated, with an example invocation
- [ ] No secrets or personal data: run `gitleaks dir .` and grep for student IDs/emails, course codes from your own account, cookies, local paths
- [ ] Version bumped in `pyproject.toml`; tag `vX.Y.Z` and a GitHub release with notes
- [ ] CI green on the tagged commit
