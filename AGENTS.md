# moodlectl agent guide

This repository is building toward agent-friendly CLI ergonomics.

## Product intent

`moodlectl` is a lean Moodle CLI for:
- dashboard/course inspection
- assignment drilldown
- announcements
- file fetching
- deadline export/sync
- structured downstream consumption by agents

Keep the tool browser-light for the normal path. Browser automation is acceptable for login/bootstrap, not for everyday reads.

## Output design rules

When adding or changing read commands:
- keep JSON as the canonical machine format
- support `--format text|json|toon` where practical
- preserve `total`, `summary`, and `next_commands` in structured output
- prefer minimal defaults over giant payloads
- truncate long structured fields unless `--full` is set
- keep stdout for command payloads; status/logging belongs on stderr where possible
- use `--fields` to support narrower agent consumption without inventing bespoke subcommands

## Auth and credential rules

Never print the MoodleSession cookie.

Prefer these auth entry paths over raw shell history leakage:
1. `moodlectl auth set-cookie --stdin`
2. `moodlectl auth import-cookie-file <path>`
3. `MOODLE_SESSION` environment variable

Avoid introducing commands that require interactive prompts.

## Implementation preferences

- Add tests before changing behavior.
- Favor small helper modules over growing `cli.py` further.
- Prefer deterministic parsers for cookie imports, HTML cleanup, and structured output.
- If adding a new auth source, document the resolution order and security tradeoffs.

## Release checklist for agent-facing features

- parser updated
- docs updated
- tests added/updated
- stdout/stderr behavior checked
- example invocation included in README or docs
