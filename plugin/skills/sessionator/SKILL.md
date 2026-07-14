---
name: sessionator
description: Search the user's unified local history of past Claude Code and Codex sessions — dates, projects, five-field summaries, files touched, commits, and the model used — and hand back a ready-to-run command to resume any one. Use when the user asks when they worked on something, wants to find a past session, asks whether they already did something, or wants to resume or restore context from earlier work. Triggers include "when did I work on X", "find that session where…", "which sessions touched <repo/file>", "did I already do X", "resume that session", "pick up where I left off", "/sessionator", and "check my session history". Wraps the local `sessionator` CLI; never read raw transcript directories for these questions.
---

# sessionator — search past sessions and resume them

`sessionator` is a local command-line tool over a canonical session store: one
record per interactive session across **both harnesses** (Claude Code and Codex),
each carrying its date, cwd, repo, branch, a five-field summary
(Asked / Learned / Completed / Left off / Next steps), the files it touched,
commits, PRs, keywords, skills, model, and a pointer to the raw transcript.

Everything runs and stays on the user's machine. Ingestion is reconciled
automatically before every query, so a search always reflects the just-finished
session — there is no freshness caveat to warn about.

**Zero LLM in the search path.** Parse the user's ask into flags, run the CLI,
present the hits. The tool is deterministic; treat it as the source of truth
rather than searching transcript directories directly.

## Prerequisite

The skill assumes the `sessionator` CLI is installed and on `PATH`
(`uv tool install git+https://github.com/edbrodie/sessionator`, or run without
installing via `uvx --from git+https://github.com/edbrodie/sessionator sessionator …`).
If a command is not found, or results look wrong, run `sessionator status` as the
health check — it prints config paths, detected sources, record counts, and the
search engine in use.

## Mapping the ask to a command

Search is the default: a bare invocation with free-text terms *is* a search. All
terms are ANDed and case-insensitive, so start broad (fewest terms) and narrow
with filters.

| When the user asks | Run |
|---|---|
| "when did I work on the bubble charts" | `sessionator bubble chart` |
| "what did I do yesterday / on 5 June" | `sessionator --since 2026-06-05 --until 2026-06-05 --format full` |
| "which sessions were in the website repo" | `sessionator --repo website` (or `--cwd website` to catch worktrees) |
| "that Codex session where…" | `sessionator <terms> --harness codex` |
| "anything I left unfinished on X" | `sessionator <terms> --resolved open` |
| "what did opus / gpt-5.6 do on X" | `sessionator <terms> --model opus` |
| "show me that session in full" | `sessionator show <sid-prefix>` |
| "resume that session" / "pick up where I left off" | `sessionator resume <sid-prefix>` |

**Filters:** `--keyword` (exact, repeatable), `--repo`, `--cwd`, `--model`,
`--harness {claude,codex}`, `--since`/`--until` (`YYYY-MM-DD`, inclusive),
`--resolved {open,done,unknown}`, `--limit` (default 20, `0` = all),
`--format {compact,full,ndjson}`.

Searched fields: the summary, keywords, cwd, repo, branch, commit subjects, file
paths, and skills.

**Exit codes** (grep-style, so you can branch without parsing text):
`0` = hits, `1` = zero hits, `2` = error. Diagnostics and the match count go to
stderr; stdout is data only.

## Presenting hits

- Default to `--format compact` to scan: each hit is one row —
  `date · harness(model) · cwd-tail · sid · asked`. A summary still being
  generated shows `(summary pending)`; the record is fully searchable regardless.
- Reach for `--format full` when the user wants detail — it prints all five
  summary fields, the actual file paths and commit SHAs, open todos, and the
  resume line.
- Session ids are `harness/uuid` (for example `claude/d78b1004`). Any unambiguous
  prefix resolves a session; if a prefix matches more than one, the CLI lists the
  candidates so you can disambiguate.

## Resuming past work

When the user asks to resume past work, retrieve the exact invocation rather than
reconstructing it:

1. Search, identify the intended session, then run `sessionator resume <sid-prefix>`.
2. The output is a single, copy-pasteable line that changes into the session's
   directory and re-opens it in its own harness — for example
   `cd /path/to/repo && claude --resume <id>` or `cd /path/to/repo && codex resume <id>`.
3. Relay that line for the user to run. For a fuller handoff, `sessionator show
   <sid-prefix>` adds the record's Left-off note, open todos, files manifest, and
   branch alongside a tail of the raw transcript — the resume packet.

## Diagnostics

`sessionator status` is the read-only doctor: it reports the config and data-store
paths, which harness sources were detected, per-harness record counts, how many
summaries are pending or errored, and the active search engine. Use it to confirm
the tool is healthy before concluding that a session is genuinely absent.
