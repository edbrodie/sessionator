# Index & ingestion runtime design

How transcripts on disk become a current, searchable index — and how summaries
are produced without ever blocking a query or a session-end hook.

## Storage layout

Everything lives under the data dir (`$XDG_DATA_HOME/sessionator`, else
`~/.local/share/sessionator`):

- `store.jsonl` — one `Record` per line, keyed by `sid`, rewritten atomically
  (temp + rename), sorted date-desc then last_active-desc.
- `transcripts/<harness>-<uuid>.md` — the capped, `<private>`-stripped excerpt
  sidecar; pruning-proof, so `show` still has content after a harness prunes the
  live transcript.
- `watermarks.json` — `{transcript_path: [mtime, size]}` for the reconcile scan.
- `tombstones.json` — sids `forget` has retired; reconcile never re-ingests them.
- `store.lock` / `backfill.lock` — flock files (see Locking).

## Record

Schema v1 (see `sessionator.schema`). Beyond the T-001 field set, three
operational fields the runtime requires: `summary_state`
(`pending`/`stale`/`done`/`error`), `parse_warnings` (int), and `forked_from`
(Codex lineage, else null).

## Reconcile (deterministic ingest)

`sessionator ingest` runs the inline, LLM-free pass:

1. **Retroactive exclusion purge** — drop any stored record whose cwd now
   matches an exclusion glob. Adding an exclusion removes data, not just future
   data.
2. **Watermark scan** — for each adapter, enumerate candidates; skip any file
   whose `(mtime, size)` is unchanged since last seen. Extract new/changed files.
3. **Upsert** — filtered sessions, excluded cwds, and tombstoned sids are
   recorded in the watermark but not stored. A new session is `pending`; a
   changed known session is rebuilt and marked `stale` (its previous summary
   stays visible until re-summarized).
4. **Kick the backfill** — spawn the detached summary process and return
   immediately.

The store/watermark mutation runs under the store-write lock; extraction runs
outside it.

## Summary execution — detached one-shot backfill

There is **no daemon**. The reconcile path (and, in the eventual plugin, a
SessionEnd hook) writes the deterministic record fast, then spawns a detached,
short-lived backfill that batch-summarizes every summary-less/stale record and
exits. Queries never wait; a very fresh record may briefly show as pending.

- **Batching**: up to ~8 sessions per CLI call, using the `@@S<n>@@` marker
  pattern to split one response back into per-session results. Input per session
  is the capped, `<private>`-stripped excerpt.
- **Summarizer mapping**: Claude sessions → `claude -p` (default `opus-4.8`,
  effort medium); Codex sessions → `codex exec` (default `gpt-5.6-luna`,
  reasoning high). One CLI installed → it summarizes both. None installed →
  records stay deterministic-only and remain searchable.
- **Five fields + resolved**: the model returns `asked`, `learned`, `completed`,
  `left_off`, `next_steps`, and a refined `resolved` tri-state.
- **Failure**: a summarizer error leaves the record summary-less (state `error`)
  and is retried on the next backfill. It never blocks or fails a query.

Auth/stdin handling: the `claude -p` call unsets `ANTHROPIC_API_KEY` /
`ANTHROPIC_AUTH_TOKEN` (so it uses interactive session credentials) and passes
`--no-session-persistence` (so it does not self-pollute `~/.claude/projects`);
the `codex exec` call closes stdin (`stdin=DEVNULL`).

## Locking

One store-write lock guards the load → mutate → write critical section; reconcile
and the backfill's write-back take it blocking (with a timeout). Reads take no
lock (append-only JSONL, atomic rewrite). A separate single-instance
`backfill.lock` is taken non-blocking at backfill start, so a second backfill
exits at once.

## Config

XDG TOML at `$XDG_CONFIG_HOME/sessionator/config.toml` (`~/.config` fallback),
auto-written on first run: it auto-detects the CLIs and transcript dirs, writes
detected values plus shipped summarizer defaults, and prints what it found — no
wizard. Read with stdlib `tomllib`; written by a tiny hand-rolled emitter.
`status` displays the live config. Exclusion globs are edited here; the worked
example is Ed's local-only vault rule, `**/private-notes/**`.
