# Authentication

moodlectl reads Moodle with a `MoodleSession` cookie. There are two ways to get one:

1. **`moodlectl login`** (recommended). It drives the UofG Microsoft SSO login in a headless browser. Install it with the `[login]` extra.
2. **Paste a cookie** from your own browser session with `moodlectl auth set-cookie`.

## `moodlectl login`

```bash
moodlectl login                        # interactive: asks for anything it needs, prints the MFA number
moodlectl login --if-needed            # no-op (exit 0) if the current session still works
moodlectl login --if-needed --no-mfa   # unattended: never starts an MFA request (exit 3 instead)
```

What happens:

1. Opens `https://moodle.gla.ac.uk/my/` in a Camoufox (hardened Firefox) browser with a **persistent profile**.
2. On UofG's "Select your login" page, picks *UofG email address*.
3. On Microsoft's pages, enters your email and password, chooses *Approve a request*, and prints the number-matching code as `MFA_NUMBER nn` on stdout. It also writes the code to the status file.
4. Answers **yes** to "Stay signed in?". Because the profile persists, later runs usually skip the password and MFA entirely.
5. Once the Moodle dashboard loads, saves the `MoodleSession` cookie and verifies it.

### Credentials

The login only needs your password when Microsoft's own session has expired. Credentials are resolved in this order:

1. `MOODLE_USERNAME`, `MOODLE_PASSWORD` (and `UOFG_GUID` for the timetable feed) environment variables
2. saved credentials: `moodlectl auth set-credentials` → `$MOODLECTL_SECRETS_DIR/credentials.json` (mode 600)
3. an interactive prompt, if there's a terminal

`moodlectl auth forget-credentials` deletes the saved credentials and the SSO browser profile.

### MFA safety

- **Exactly one push per run.** On "Request denied", "We didn't hear from you" or "Request wasn't sent", the run stops with exit 4. It never auto-resends: repeated pushes look like an MFA-fatigue attack, and Microsoft throttles the account. Microsoft also shows "Request denied" for requests nobody answered.
- After a failed MFA request, new runs refuse to start for 10 minutes unless you pass `--force`.
- A file lock stops two logins (e.g. cron + an agent) from running at once and sending two pushes (exit 6).
- A wrong username or password stops at once (exit 5) to avoid locking the account.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | logged in (or `--if-needed` and already live) |
| 1 | setup problem: `[login]` extra missing, browser can't start, credentials needed but no terminal |
| 2 | timed out, or got a cookie that Moodle rejected |
| 3 | MFA required but `--no-mfa` was given |
| 4 | MFA denied / expired / throttled (no resend) |
| 5 | wrong username or password |
| 6 | MFA cooldown active, or another login is running |

### Status file

`moodlectl login` keeps non-secret state in `$MOODLECTL_CONFIG_DIR/login-status.json` (override with `MOODLECTL_AUTH_STATUS_FILE`). It records `moodle_live`, `moodle_login_at`, `mfa_pending`, `mfa_number` and `needs_human` (`mfa`, `mfa-denied-or-expired`, `mfa-throttled`, `bad-credentials`, `login-timeout`, `verify-failed`). `moodlectl status` shows it. Agents can poll it to pass an MFA number on to a human.

### Keeping agents away from your password

By default, credentials and the SSO profile live in the config dir next to the cookie. If an agent runs moodlectl in a sandbox, put the secrets somewhere the sandbox can't see. Run `login` outside the sandbox and share only the config dir (cookie + status):

```bash
export MOODLECTL_SECRETS_DIR=~/.local/share/moodlectl-secrets   # host only
moodlectl login --if-needed --no-mfa                             # e.g. from cron every 3 hours
```

### Platform notes

- The first run downloads the Camoufox browser (~1.3 GB) into the user cache dir.
- `camoufox` is pinned to `<0.5`. The 0.5 line currently fails fingerprint generation, and installing it replaces a 0.4 browser in the shared cache.
- Bare Linux servers/containers need Firefox's system libraries: `python -m playwright install-deps firefox`.
- Running as root: moodlectl unsets `XDG_RUNTIME_DIR` before launching, because Firefox refuses a runtime dir owned by another user.

## Pasting a cookie instead

```bash
moodlectl auth set-cookie                       # hidden prompt
printf '%s' "$COOKIE" | moodlectl auth set-cookie --stdin
moodlectl auth import-cookie-file cookies.txt    # export from a browser extension
```

`set-cookie` and `import-cookie-file` accept any of these:

- the bare value
- `MoodleSession=<value>`
- a full `Cookie:` header
- a Netscape `cookies.txt`
- a browser JSON cookie export (a list of `{name, value}` objects, or `{"cookies": [...]}`)

After saving, moodlectl checks the cookie against `/my/` straight away. `moodlectl auth set-cookie <value>` also works. Avoid it on shared machines, because the value lands in your shell history.

## Resolution order, checking, clearing

- Cookie: `MOODLE_SESSION`, then `$MOODLECTL_CONFIG_DIR/config.json` (default `~/.config/moodlectl/config.json`, mode 600).
- Site: `MOODLE_BASE_URL`, then the saved `base_url`, then `https://moodle.gla.ac.uk`.

```bash
moodlectl auth status            # where the cookie comes from + live check (exit 2 if expired)
moodlectl auth status --offline  # no network
moodlectl auth clear             # forget the stored cookie
```

When Moodle rejects the session, which includes any redirect to the Microsoft/UofG login pages, every command exits with **code 2** and prints `AUTH_EXPIRED: …` on stderr. Set `MOODLECTL_REAUTH_HINT` to customise that message for your setup.

## Security notes

- The cookie gives full access to your Moodle account while it's valid. The saved credentials are your university password. Treat both accordingly, and keep the secrets dir out of backups and sandboxes you don't trust.
- Nothing secret is printed. Status and config files never contain your password.
- Never commit `config.json`, `credentials.json`, cookie exports or `.env` files. The repo's `.gitignore` covers the common names.
