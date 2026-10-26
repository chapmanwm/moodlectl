# Authentication

moodlectl reads Moodle with your `MoodleSession` cookie. It doesn't handle your UofG password or MFA, and it never asks for them.

## Where the cookie comes from

Every command resolves auth in this order:

1. `MOODLE_SESSION` environment variable
2. `$MOODLECTL_CONFIG_DIR/config.json` (default `~/.config/moodlectl/config.json`)

The Moodle site comes from `MOODLE_BASE_URL`, then the `base_url` saved in `config.json`, then `https://moodle.gla.ac.uk`.

## Saving a cookie

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

After saving, moodlectl checks the cookie against `/my/` straight away and tells you whether it works.

`moodlectl auth set-cookie <value>` also works. Avoid it on shared machines, because the value lands in your shell history.

## Checking and clearing

```bash
moodlectl auth status            # where the cookie comes from + live check (exit 2 if expired)
moodlectl auth status --offline  # no network
moodlectl auth clear             # forget the stored cookie
```

`config.json` is written with mode 600. The cookie value is never printed.

## Expiry

The session lasts roughly as long as a browser login. When Moodle rejects it, which includes any redirect to the Microsoft/UofG login pages, every command exits with **code 2** and prints `AUTH_EXPIRED: …` on stderr. Log in again in your browser and save the new cookie.

If you've automated the refresh yourself (e.g. a browser-automation script that logs in and pipes the cookie into `auth set-cookie --stdin`), set `MOODLECTL_REAUTH_HINT` to tell agents what to run. Optionally, have the script write a status JSON and point `MOODLECTL_AUTH_STATUS_FILE` at it, so `moodlectl status` shows its state. moodlectl itself deliberately doesn't automate the SSO/MFA login.

## Security notes

- The cookie gives full access to your Moodle account for as long as it's valid. Treat it like a password.
- Don't commit `config.json`, cookie exports or `.env` files. The repo's `.gitignore` covers the common names.
- For agents, prefer injecting `MOODLE_SESSION` from a secret store over writing it to disk.
