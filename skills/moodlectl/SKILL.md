---
name: moodlectl
description: Use when the user asks about their University of Glasgow Moodle coursework or timetable — deadlines, assignment briefs, lecture slides, announcements, grades, classes, or what changed. Reads a local mirror kept fresh by the moodlectl CLI.
---

# moodlectl (UofG Moodle)

`moodlectl` mirrors the user's Moodle courses into local files. Answer from those files and refresh them with the CLI.

## First, check it works

```bash
moodlectl --json status
```

`corpus` is the root of the local mirror. If `moodle` is `EXPIRED…` (or any command exits `2`):

1. Run `moodlectl login --if-needed` **once** and watch its output.
2. If it prints `MFA_NUMBER nn`, tell the user straight away, as the first line of your reply: "Approve Moodle login in Microsoft Authenticator: **nn**". The number expires in a few minutes.
3. Exit codes: `0` logged in. `3`/`4` MFA needed, denied or expired: tell the user and wait until they say they're ready. **Never re-run in a loop**, because repeated pushes get the account throttled. `5` wrong credentials: ask the user to run `moodlectl auth set-credentials` themselves. `6` cooldown or another login running: wait. `1` setup problem: show the message.
4. Never ask for the password or cookie in chat.

## Typical questions

| User asks | Run |
|---|---|
| "What's due?" | `moodlectl --json todo --days 21` |
| "What classes do I have this week?" | `moodlectl timetable --days 7` |
| "Anything new on Moodle?" | `moodlectl --json changes --since 24h`, `moodlectl --json news --since 7d` |
| "What does the brief for X say?" | `moodlectl find '<X>'`, then read `<corpus>/<CODE>/moodle/pages/*.md` and `text/*` |
| "Explain lecture N of COURSE" | read `<corpus>/<CODE>/moodle/COURSE.md` to find the file, then its `text/` extract |
| "Did my submission go through?" | `moodlectl --json assignment <cmid-or-url>` (live) |
| "My grades?" | `moodlectl --json grades` |

If the mirror is stale (look at `last_sync` in `status`), run `moodlectl sync` first. It's incremental, but the first run takes a few minutes. Times from `todo` and `timetable` are already in UK local time; don't convert them.

## Corpus layout

```
<corpus>/<CODE>/moodle/COURSE.md         assessments table + all section text + links to files
<corpus>/<CODE>/moodle/ANNOUNCEMENTS.md
<corpus>/<CODE>/moodle/GRADES.md
<corpus>/<CODE>/moodle/pages/            assignment / quiz / page details
<corpus>/<CODE>/moodle/files/            original downloads
<corpus>/<CODE>/moodle/text/             extracted text of every file — read these, not the PDFs
<corpus>/_moodle/changes.jsonl           change log
<corpus>/_moodle/timetable.md            last timetable render
```

## Rules

- Course pages keep last year's dates in titles and slides after rollover. Take dates from `todo` / `pages/`, and say when a date is inferred.
- Quote the brief when giving requirements (word limits, formats, deadlines). Don't paraphrase from memory.
- Never print, log or ask for the cookie or password. `moodlectl auth status` and `status` are safe to show.
- Don't run `sync` more often than needed.
