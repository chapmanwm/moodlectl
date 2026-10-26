---
name: moodlectl
description: Use when the user asks about their University of Glasgow Moodle coursework — deadlines, assignment briefs, lecture slides, announcements, grades, or what changed. Reads a local mirror kept fresh by the moodlectl CLI.
---

# moodlectl (UofG Moodle)

`moodlectl` mirrors the user's Moodle courses into local files. Answer from those files and refresh them with the CLI.

## First, check it works

```bash
moodlectl --json status
```

- Exit code `2` or `"moodle": "EXPIRED…"` means the session cookie expired. **Stop.** Ask the user to log in to moodle.gla.ac.uk in their browser, copy the `MoodleSession` cookie, and run `moodlectl auth set-cookie`. Never ask them to paste the cookie into chat. Don't retry in a loop.
- `corpus` is the root of the local mirror.

## Typical questions

| User asks | Run |
|---|---|
| "What's due?" | `moodlectl --json todo --days 21` |
| "Anything new on Moodle?" | `moodlectl --json changes --since 24h`, `moodlectl --json news --since 7d` |
| "What does the brief for X say?" | `moodlectl find '<X>'`, then read `<corpus>/<CODE>/moodle/pages/*.md` and `text/*` |
| "Explain lecture N of COURSE" | read `<corpus>/<CODE>/moodle/COURSE.md` to find the file, then its `text/` extract |
| "Did my submission go through?" | `moodlectl --json assignment <cmid-or-url>` (live) |
| "My grades?" | `moodlectl --json grades` |

If the mirror is stale (look at `last_sync` in `status`), run `moodlectl sync` first. It's incremental, but the first run takes a few minutes.

## Corpus layout

```
<corpus>/<CODE>/moodle/COURSE.md         assessments table + all section text + links to files
<corpus>/<CODE>/moodle/ANNOUNCEMENTS.md
<corpus>/<CODE>/moodle/GRADES.md
<corpus>/<CODE>/moodle/pages/            assignment / quiz / page details
<corpus>/<CODE>/moodle/files/            original downloads
<corpus>/<CODE>/moodle/text/             extracted text of every file — read these, not the PDFs
<corpus>/_moodle/changes.jsonl           change log
```

## Rules

- Course pages keep last year's dates in titles and slides after rollover. Take dates from `todo` / `pages/`, and say when a date is inferred.
- Quote the brief when giving requirements (word limits, formats, deadlines). Don't paraphrase from memory.
- Never print, log or ask for the cookie. `moodlectl auth status` is safe to show.
- Don't run `sync` more often than needed.
