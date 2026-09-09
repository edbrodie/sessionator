# sessionator

A unified, searchable history of your Claude Code and Codex sessions — one local
store you can grep by date, project, topic, files touched, or model, that hands
back a ready-to-run command to resume any session where you left off.

**Local, private, zero network calls, no telemetry — [enforced by a CI test](tests/test_no_network.py).**
Everything runs and stays on your machine. sessionator reads only your own
transcript files and never phones home.

## Install and run

No install — run it straight from the repo with [uv](https://docs.astral.sh/uv/):

```console
$ uvx --from git+https://github.com/edbrodie/sessionator sessionator "auth bug"
```

Or install the `sessionator` command onto your PATH:

```console
$ uv tool install --from git+https://github.com/edbrodie/sessionator sessionator
$ sessionator "auth bug"
```

There is no PyPI package and no config wizard. The first run auto-detects your
`claude`/`codex` transcript directories, writes a config file, and prints what it
found — no questions asked.

## What it does

One record per interactive session, across **both** harnesses. Search is the
default verb: a bare invocation with free-text terms *is* a search (terms are
ANDed, case-insensitive). Results follow grep's exit codes — `0` hits, `1` no
hits, `2` error — so it scripts cleanly.

```console
$ sessionator auth
2026-07-10  Claude(opus-4.8)   dashboard  claude/9f3a2c71  Fix the login redirect loop after token refresh
2026-06-28  Codex(gpt-5.6)     api        codex/1b8e04d2   Add JWT auth middleware to the REST handlers
2 matches
```

Narrow with filters — `--repo`, `--cwd`, `--model`, `--harness {claude,codex}`,
`--since`/`--until` (`YYYY-MM-DD`), `--resolved {open,done,unknown}`,
`--keyword` (exact, repeatable), `--limit`, `--format {compact,full,ndjson}`:

```console
$ sessionator --repo dashboard --resolved open --since 2026-07-01
```

`sessionator show <sid-prefix>` prints the full record — the five-field summary,
the actual files and commits, tests, PRs, a transcript tail, and the resume line.
Any unambiguous sid prefix works:

```console
$ sessionator show claude/9f3a2c71
● claude/9f3a2c71-...
  2026-07-10 · Claude (opus-4.8) · resolved=done
  cwd:    /home/you/dashboard
  repo:   you/dashboard (branch: main)
  summary:
    Asked: Fix the login redirect loop after token refresh
    Learned: the refresh handler dropped the return-to param on 401
    Completed: patched the handler, added a regression test
    Left off: shipped; PR merged
    Next steps: None
  files (1):
    M src/auth/refresh.ts
  commits (1):
    ab12345 fix auth redirect loop
  tests: 12 passed (broken=False)
  resume: cd /home/you/dashboard && claude --resume 9f3a2c71-...
```

`sessionator resume <sid-prefix>` prints exactly one copy-pasteable line that
changes into the session's directory and re-opens it in its own harness:

```console
$ sessionator resume claude/9f3a2c71
cd /home/you/dashboard && claude --resume 9f3a2c71-...
```

`sessionator summarize <sid-prefix>` summarizes one session right now, in the
foreground, instead of waiting for the background pass.

`sessionator status` is the read-only doctor — config paths, detected sources,
record counts, pending summaries, segment counts, and the active search engine.
`sessionator setup codex` installs the Codex capture hooks and `sessionator setup
status` reports how capture is wired up on this machine (see
[Capture](#how-it-works)). `sessionator forget <sid|cwd-glob>` retires sessions
from the store (see [Privacy](#privacy)).

## How it works

**Capture.** Two hooks per harness, both piping their payload into `sessionator
ingest --hook`:

| Harness | Hooks | Installed by |
|---------|-------|--------------|
| Claude Code | `PreCompact` (async) + `SessionEnd` | the [plugin](plugin/README.md) |
| Codex (CLI **and** desktop app) | `PreCompact` (async, 600 s) + `SessionEnd` (3 s) | `sessionator setup codex` |

`ingest --hook` writes the payload to a spool file, hands it to a detached worker,
and returns in milliseconds — well inside Codex's 3 s synchronous `SessionEnd`
budget. It prints nothing and always exits 0, because a capture tool that can
break your session is worse than one that misses a session. The worker does the
real work out of band: ingest that one transcript, cut a summary segment labelled
with the event, summarize it.

Codex has no plugin system, so its hooks are a file you own:

```console
$ sessionator setup codex --dry-run   # show the exact change
$ sessionator setup codex             # merge it into ~/.codex/hooks.json
```

Then run `/hooks` inside Codex to trust the new entries. The merge only ever adds
or rewrites its own two handlers, appends new groups at the end so no existing
hook's positional trust hash moves, and never reads or writes `config.toml` —
where your own hooks live. `sessionator setup codex --remove` takes them back
out. `SessionEnd` on Codex fires on archive, delete, close, **and 30 minutes of
idle**, which is what gets long-lived Codex threads into the index at all.

Nothing runs as a daemon and nothing is scheduled; `sessionator ingest` is still
just a command you can run yourself, and every query reconciles anyway.

**Reconcile.** Every query first runs a fast, non-blocking reconcile that scans
your transcript directories, writes a deterministic record for anything new or
changed, and never waits on summaries — so a search always reflects the session
you just finished.

**Store + index.** The truth is a plain append-friendly `store.jsonl` under
`$XDG_DATA_HOME/sessionator` (`~/.local/share/sessionator`); search runs over a
derived SQLite **FTS5** index (with trigram matching where your SQLite supports
it, else a porter-only or linear-scan fallback). The index is derived — delete it
and the next query rebuilds it.

**Summaries.** The five fields (Asked / Learned / Completed / Left off / Next
steps) are built **incrementally**. Each hook cuts a *summary segment* over the
turns since the last cut, and one short model call folds that segment into the
running summary. A session that compacts five times gets five cheap calls over
what actually happened, instead of one call over a transcript whose middle the
harness has already thrown away. `summary` stays the rolled-up five fields —
search, `show` and the index are unchanged — and `summary_segments` is the audit
trail behind it. A session no hook ever saw still gets one whole-transcript
summary, and a Codex rollout's own `compacted` markers are replayed as segment
boundaries.

The call shells out to whichever agent CLI you already have, using your existing
**subscription login**, not an API key. The default is **Haiku** via `claude -p
--model haiku` for *both* harnesses: summaries are small, frequent and
per-segment, so the cheapest capable model is the right one. `codex exec` is the
fallback when `claude` is not installed. Change the preference with
`[summarize].prefer` in your config — `"claude"` (default), `"codex"`, or
`"same"` (summarize each session with its own harness's CLI, the pre-v2
behaviour).

No summarizer installed? Records are still fully captured and searchable; the
summary stays pending and fills in the next time a CLI is around.

## Requirements

- **Python 3.11+** and [uv](https://docs.astral.sh/uv/). Zero runtime
  dependencies — the CLI is standard library only.
- **Optional:** the `claude` and/or `codex` CLIs on your PATH, for summary
  generation. Everything else works without them.

## Claude Code plugin

The companion plugin adds a `/sessionator` skill (search and resume from inside a
conversation) plus the `SessionEnd` capture hook. Once the repository is public:

```
/plugin marketplace add edbrodie/sessionator
/plugin install sessionator
```

Or point the marketplace at a local checkout:

```
/plugin marketplace add /path/to/sessionator
/plugin install sessionator
```

The plugin is a thin wrapper — it shells out to the `sessionator` CLI, so install
the CLI first. See [plugin/README.md](plugin/README.md) for the full hook
behavior and uninstall steps.

## Privacy

sessionator is built to keep sensitive work out of the store, and to let you pull
it back out if it slips in.

- **Zero network.** The package imports nothing that can open a socket and shells
  out only to an approved, runtime-resolved CLI path — both asserted statically by
  [`tests/test_no_network.py`](tests/test_no_network.py) on every CI run.
- **Directory exclusions.** Add `cwd_globs` to the `[exclusions]` block in your
  config to keep whole trees out of the index — for example a local-only notes
  vault. Excluded sessions are purged retroactively when you add the glob, and any
  matching path is scrubbed wherever it appears.
- **`<private>` redaction.** Anything a transcript wraps in `<private>…</private>`
  is stripped from the excerpt before it is ever stored or handed to a summarizer.
- **`forget`.** `sessionator forget <sid-prefix>` or `forget '<cwd-glob>'` deletes
  the record and its excerpt and writes a tombstone so ingest won't re-add it. It
  never touches your original harness transcript. `--dry-run` previews.
- **What the store holds:** session metadata (date, cwd, repo, branch, model),
  the five-field summary, file paths and commit subjects, and a capped,
  private-stripped transcript excerpt — all under
  `$XDG_DATA_HOME/sessionator`. Delete that directory to remove everything.

## FAQ

**Does it phone home or send telemetry?** No. Zero network calls, no analytics, no
update checks — and it's a test, not a promise: see
[`tests/test_no_network.py`](tests/test_no_network.py).

**What about my private notes / vault?** Exclude the directory with a `cwd_globs`
entry (retroactively purged), wrap sensitive spans in `<private>…</private>`, or
`forget` a session after the fact. Excluded paths are also scrubbed wherever they
appear in other records.

**What if I only have one of the two CLIs?** Fine. sessionator captures and
searches both harnesses' sessions regardless; a session is summarized by whichever
CLI you have, falling back to the other. With neither installed, summaries stay
pending and everything else works.

**Do I need an API key?** No. Summary generation uses your existing Claude/Codex
subscription login via the local CLI.

**Does it modify my transcripts?** Never. sessionator only reads your harness
transcript files. It writes exclusively to its own store under
`$XDG_DATA_HOME/sessionator`.

## Breaking and migration notes

Upgrading an existing install is automatic, but three things changed shape:

- **Config schema v2.** `[summarize].claude.model` now defaults to `haiku`, and
  `[summarize].prefer` is new. On first load, a v1 config still carrying the old
  shipped default (`opus-4.8`) is migrated to `haiku` and rewritten; a model you
  chose yourself is left alone.
- **Watermarks v2.** `watermarks.json` is now
  `{"schema": 2, "entries": {key: [mtime, size]}}`, keyed by a stable session
  identity (`codex:<uuid>`, `claude:<stem>`) rather than the transcript path, and
  compared on size. v1 files are re-keyed on load — nothing to do by hand. This is
  what makes archiving a Codex thread (which *moves* its rollout file) a no-op
  instead of a full re-ingest.
- **New record fields.** `summary_segments` (the per-segment audit trail) and
  `client` (the harness build: `claude-code`, `Codex Desktop`, `codex-tui`, …).
  Older records load fine and simply have neither.

One behaviour change worth knowing: Codex sessions from the **desktop app** were
previously dropped by an `originator == "codex-tui"` allowlist and are now
captured, so the first ingest after upgrading may add a lot of sessions at once.

## License

MIT — see [LICENSE](LICENSE).
