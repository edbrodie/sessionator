---
name: sessionator
description: Searches the user's local sessionator index of past Claude Code and Codex sessions and hands back a ready-to-run command to resume one. This skill should be used whenever the user asks when did I work on X, find that session where something happened, did I already do X, which sessions touched a repo or a file, resume that session, pick up where I left off, or check my session history. Not for recalling the current conversation, reading git log, or checking background tasks.
---

# sessionator: search past sessions and resume them

`sessionator` is a local command-line tool over a canonical session store: one
record per interactive session across both harnesses (Claude Code and Codex),
each carrying its date, cwd, repo, branch, a five-field summary
(Asked / Learned / Completed / Left off / Next steps), the files it touched,
commits, PRs, keywords, skills, model, and a pointer to the raw transcript.

Everything runs and stays on the user's machine. Ingestion is reconciled before
every query, so a search always reflects the session that just ended. There is
no freshness caveat to warn about.

Do not reason about transcripts yourself. Parse the ask into flags, run the CLI,
present the hits. The tool is deterministic. Treat it as the source of truth
instead of grepping transcript directories.

## Before running anything

Run the intended command directly. Do not probe with `--version` first.

If the shell reports `command not found`, the CLI is not installed. Say so and
offer the no-install invocation, then stop and wait:

```
uvx --from git+https://github.com/edbrodie/sessionator sessionator status
```

Never install the CLI unprompted, and never fall back to reading transcript
directories by hand.

If the CLI runs but the results look wrong (empty store, one harness missing,
stale dates), run `sessionator status` and relay what it reports.

## Always use an explicit verb

Write `sessionator search TERMS`, never bare `sessionator TERMS`. A bare
invocation is treated as a search only when its first word is not a verb, so
`sessionator show me the auth work` parses `show` as the `show` verb and fails
or resolves the wrong thing. The reserved first words are `search`, `show`,
`resume`, `ingest`, `status`, `forget`, `summarize`, and `setup`.

## Resolve relative dates before calling

The CLI takes absolute `YYYY-MM-DD` bounds only. Convert the user's phrasing
using today's date, and make single days a closed range:

- "yesterday" becomes `--since D --until D` for that one date.
- "last week" becomes the Monday-to-Sunday range, or the trailing seven days
  when the user means recency rather than a calendar week.
- "in June" becomes `--since 2026-06-01 --until 2026-06-30`.
- "since the sprint started" needs one clarifying question, not a guess.

## Map the ask to a command

| When the user asks | Run |
|---|---|
| "when did I work on the bubble charts" | `sessionator search bubble chart` |
| "what did I do on 5 June" | `sessionator search --since 2026-06-05 --until 2026-06-05 --format full` |
| "which sessions were in the website repo" | `sessionator search --repo website` |
| "that Codex session where the build broke" | `sessionator search build broke --harness codex` |
| "anything I left unfinished on the index" | `sessionator search index --resolved open` |
| "what did opus do on the parser" | `sessionator search parser --model opus` |
| "show me that session in full" | `sessionator show SID-PREFIX` |
| "resume that session" | `sessionator resume SID-PREFIX` |

Terms are ANDed and case-insensitive. Start with the fewest terms and narrow
with flags.

## When nothing comes back

Exit code `1` means zero hits, not an error. Climb the ladder one rung at a
time, reporting what changed:

1. Drop the narrowest filters (`--model`, `--resolved`, then the date bounds).
2. Drop free-text terms down to the single most distinctive word.
3. Swap `--repo NAME` for `--cwd NAME`, which also matches worktrees and clones
   whose directory name differs from the repo name.
4. Run `sessionator status` to confirm the store is populated and both harnesses
   were detected.
5. Only after all four, tell the user the session is genuinely not in the index.

Exit code `2` is a real error. Surface stderr verbatim and stop.

## Present the hits

Relay compact rows verbatim, newest first. Each row is
`date · harness(model) · cwd-tail · sid · asked`. Do not rewrite, re-sort, or
reformat them into prose.

`(summary pending)` in place of the Asked line is normal on a session that just
ended. Say the summary is still being generated; the record is fully searchable
regardless.

Reach for `--format full` when the user wants detail, or when a single hit is
clearly the answer. It prints all five summary fields, file paths, commit SHAs,
open todos, and the resume line.

Session ids are `harness/uuid`, for example `claude/d78b1004`. Any unambiguous
prefix resolves a session. When a prefix is ambiguous the CLI lists the
candidates; present them and ask which one.

## Resume past work

Retrieve the exact invocation instead of reconstructing it:

1. Search, identify the intended session, then run `sessionator resume PREFIX`.
2. Relay the single line it prints inside a fenced block, for the user to run.
   Do not execute it: resuming replaces the current session.
3. For a fuller handoff, add `sessionator show PREFIX`, which prints the
   Left-off note, open todos, files manifest, and branch.

## Fresh summaries and deletion

Summaries are generated automatically by the capture hooks. Run
`sessionator summarize FULL-SID` only when the user explicitly asks for a fresh
or missing summary; it spends a local Haiku call through the installed `claude`
CLI. Never run `sessionator forget` unless the user asks for deletion in so many
words, and confirm the target first: it writes tombstones that survive
re-ingest.

## Additional Resources

For the full flag tables, exit-code contract, output shapes, segment and summary
states, and the `setup codex` flow, consult
`${CLAUDE_PLUGIN_ROOT}/skills/sessionator/references/cli-reference.md`.
