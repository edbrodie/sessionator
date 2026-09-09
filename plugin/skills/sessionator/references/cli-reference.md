# sessionator CLI reference

Local-only tool over a canonical session store: one record per interactive
session, across Claude Code and Codex. Every command reconciles new transcripts
before it answers, so results always include the session that just ended.

## Verbs

| Verb | Purpose |
|---|---|
| `search TERMS [FLAGS]` | Query the index. |
| `show SID-PREFIX` | Full record: five-field summary, files, commits, todos, transcript tail, resume line. |
| `resume SID-PREFIX` | Print one copy-pasteable line that re-opens the session in its own harness. |
| `ingest [--no-backfill] [--hook]` | Scan transcript dirs, write records. `--no-backfill` skips summary work; `--hook` reads a hook payload from stdin (internal). |
| `status` | Read-only doctor. |
| `forget SID-OR-CWD-GLOB [--dry-run]` | Retire records (writes tombstones so re-ingest does not resurrect them). |
| `summarize SID-PREFIX [--no-wait]` | Force a fresh summary for one session. |
| `setup codex [--remove] [--dry-run]` | Write or remove Codex hook entries in `~/.codex/hooks.json`. |
| `setup status` | Report CLI path, Claude plugin hooks, Codex hook entries, spool depth, model. |

### Implicit search and the verb collision

A bare invocation with free-text terms is treated as a search, but only when the
first word is not a verb. `sessionator show me the auth work` parses `show` as a
verb, not as a search term. Always write the verb explicitly:
`sessionator search show me the auth work`.

Reserved first words: `search`, `show`, `resume`, `ingest`, `status`, `forget`,
`summarize`, `setup`.

## Search flags

| Flag | Meaning |
|---|---|
| `--keyword K` | Exact keyword match. Repeatable. |
| `--repo NAME` | Repository name. |
| `--cwd PATH` | Substring of the working directory. Catches worktrees the repo name misses. |
| `--model NAME` | Model substring, e.g. `opus`, `gpt-5.6`, `haiku`. |
| `--harness {claude,codex}` | Restrict to one harness. |
| `--since YYYY-MM-DD` | Inclusive lower date bound. |
| `--until YYYY-MM-DD` | Inclusive upper date bound. |
| `--resolved {open,done,unknown}` | Whether the work was left unfinished. |
| `--limit N` | Max hits, default `20`. `0` means all. |
| `--format {compact,full,ndjson}` | Output shape, default `compact`. |

Free-text terms are ANDed and case-insensitive. Start with the fewest terms and
narrow with flags.

Searched fields: the five-field summary, keywords, cwd, repo, branch, commit
subjects, file paths, and skill names.

## Exit codes

| Code | Meaning | What to do |
|---|---|---|
| `0` | At least one hit. | Present the rows. |
| `1` | Zero hits. This is not an error. | Broaden: drop flags, then drop terms, then swap `--repo` for `--cwd`. |
| `2` | Error. | Surface stderr to the user. Do not retry blindly. |

Stdout carries data only. The match count, warnings, and diagnostics go to
stderr, so exit codes can be branched on without parsing text.

## Output formats

`compact` (default) prints one row per hit, newest first:

```
2026-06-05 · claude(opus-4.8) · sessionator · claude/d78b1004 · Fix the flaky auth test and get CI green
```

Fields: date, harness(model), cwd tail, sid, the Asked line. A record whose
summary has not been generated yet shows `(summary pending)` in place of the
Asked line; the record is still fully searchable. `(summary pending)` is normal
on a session that just ended, not a failure.

`full` prints all five summary fields (Asked / Learned / Completed / Left off /
Next steps), the actual file paths and commit SHAs, open todos, and the resume
line. Use it when the user wants detail.

`ndjson` prints one JSON object per line, for piping.

## Session ids

Format `harness/uuid`, for example `claude/d78b1004` or `codex/9f2c1e77`. Any
unambiguous prefix resolves a session. An ambiguous prefix makes the CLI list
the candidates instead of guessing.

## show and resume output

`show` prints the record header, all five summary fields, the files manifest,
commits and PRs, open todos, branch, the segment list, and a tail of the raw
transcript. This is the handoff packet.

`resume` prints exactly one line, for example:

```
cd /Users/e/Develop/sessionator && claude --resume d78b1004-...
```

or, for a Codex record, `cd ... && codex resume ...`. Relay the line. Do not run
it: resuming replaces the current session.

## Capture and summaries

`ingest` scans the Claude Code transcript dirs and `~/.codex/sessions` plus
`~/.codex/archived_sessions`, upserts one record per session, and kicks a
detached summary backfill.

`ingest --hook` is the hook entry point. It reads the hook payload from stdin,
spools it, detaches a worker, and exits. The worker ingests only that
transcript, cuts a summary segment at the current position, and folds the new
segment into the running summary through the configured CLI: Haiku by default,
Codex when Claude is unavailable. Excerpts are sent to that provider. Segment events: `precompact`, `postcompact`, `session_end`, `stop`,
`backfill`, `change`, `manual`.

Summary states: `pending` (queued), `stale` (transcript grew since the last
summary), `partial` (some segments summarized, at least one still pending),
`done`, `error`.

`summarize SID-PREFIX` forces a manual segment covering everything not yet
summarized and runs it synchronously, printing the five fields. `--no-wait`
queues it instead. Exit 2 when no summarizer CLI is available.

## status

Reports config and data-store paths, detected harness sources, per-harness
record counts, pending/errored/partial summary counts, segment counts, the
configured summarizer model, the spool depth, and the active search engine.

## forget

Retires records by sid prefix or cwd glob (`--dry-run` previews) and writes a
tombstone so a later `ingest` does not resurrect them. Destructive. Run only on
an explicit request.

## setup codex

Writes a `SessionEnd` and an async `PreCompact` entry into `~/.codex/hooks.json`
pointing at the absolute `sessionator ingest --hook`. Never touches
`config.toml`. `--dry-run` prints the merge without writing; `--remove` unmerges.
Review new or changed hooks with `/hooks` in the Codex CLI before they run.
The shared skills plugin does not install Codex hooks, so setup is the only
Codex capture installation path. Open, idle tasks do not emit `SessionEnd`;
queries reconcile locally available transcripts.
