# Using moodlectl from an agent

This page is for wiring moodlectl into an AI agent, a script or a scheduled job.

## The model: sync, then read files

Don't ask Moodle every time the agent has a question. Instead:

1. **Sync** on a schedule (e.g. once each morning) or on demand:
   ```bash
   moodlectl sync          # all tracked courses, incremental
   ```
2. **Read the corpus.** It's plain markdown and text, which is cheap to grep and quote:
   - `<corpus>/<CODE>/moodle/COURSE.md` has the assessments table, every section's text and links to local files
   - `ANNOUNCEMENTS.md`, `GRADES.md`, `pages/*.md`, `text/*.txt`
3. **Ask about deltas** rather than re-reading everything:
   ```bash
   moodlectl --json changes --since 24h
   moodlectl --json todo --days 21
   moodlectl --json news --since 7d
   moodlectl find 'word limit|page limit' ENG1234
   ```

Find the corpus root with `moodlectl --json status` (`corpus` field), or set it explicitly with `MOODLECTL_CORPUS`.

## Live lookups

Use these only when freshness matters, e.g. checking whether a submission went through:

```bash
moodlectl --json assignment <cmid-or-url>   # dates, submission/grading status, brief, attachments
moodlectl get <pluginfile-or-resource-url> --out ./downloads
```

## Exit codes and errors

| Code | Meaning | Agent should… |
|---|---|---|
| 0 | ok | use stdout |
| 1 | error (bad course name, network, parse failure) | read stderr; don't retry blindly |
| 2 | Moodle session expired / no cookie | run `moodlectl login --if-needed` once; relay any `MFA_NUMBER nn` to the human. Never loop (see [authentication](authentication.md)). |

Payloads go to stdout and diagnostics to stderr. With `--json`, stdout is a single JSON document.

## Good habits

- Don't run `sync` in a tight loop. A first full sync makes hundreds of requests; later ones are light.
- Course pages keep last year's dates in titles and slides after rollover. Trust `todo` and `pages/` for dates.
- Read `text/` instead of opening PDFs/PPTX directly.
- Never print or log the cookie. `moodlectl auth status` is safe to show.

## Skill file

[`skills/moodlectl/SKILL.md`](../skills/moodlectl/SKILL.md) is a drop-in skill for agents that load `SKILL.md` files. Copy it into your agent's skills directory.
