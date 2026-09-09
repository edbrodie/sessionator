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
- `transcripts/<harness>-<uuid>.seg<n>.md` — one per summary segment: the slice
  that cut covered, written at cut time. The main excerpt is middle-trimmed at
  36k and the sessions that compact are exactly the long ones, so by summarizer
  time the middle would be gone; the sidecar preserves the slice verbatim.
- `watermarks.json` — `{"schema": 2, "entries": {key: [mtime, size]}}` for the
  reconcile scan. The key is the adapter's `watermark_key` (`codex:<uuid>`,
  `claude:<stem>`), not the path, so a moved transcript is still recognized;
  schema-1 files (bare, path-keyed) are re-keyed on load.
- `spool/<time_ns>-<pid>.json` — hook payloads awaiting their detached worker.
  Consumed on success, reaped after 24 h.
- `tombstones.json` — sids `forget` has retired; reconcile never re-ingests them.
- `store.lock` / `backfill.lock` / `hooks/<session>.lock` — flock files (see
  Locking).

## Record

Schema v1 (see `sessionator.schema`). Beyond the T-001 field set, the
operational fields the runtime requires: `summary_state`
(`pending`/`partial`/`stale`/`done`/`error`), `parse_warnings` (int),
`forked_from` (Codex lineage, else null), `summary_segments` (the per-segment
audit trail, see below), and `client` (the harness build that wrote the
transcript).

## Reconcile (deterministic ingest)

`sessionator ingest` runs the inline, LLM-free pass:

1. **Retroactive exclusion purge** — drop any stored record whose cwd now
   matches an exclusion glob. Adding an exclusion removes data, not just future
   data.
2. **Watermark scan** — for each adapter, enumerate candidates; skip any file
   whose **size** is unchanged since last seen. Size, not `(mtime, size)`:
   transcripts only grow, and archiving a Codex rollout changes its mtime and its
   path while the bytes stay identical. Extract new/changed files.
3. **Upsert** — filtered sessions, excluded cwds, and tombstoned sids are
   recorded in the watermark but not stored. A new session is `pending`. A
   changed session that already carries segments gets a debounced segment cut
   over just the turns that grew, and its state becomes the segment roll-up
   (`partial` while any segment is pending); one with no segments keeps the
   legacy whole-record path and is marked `stale`. Re-summarizing a long session
   from scratch on every keystroke is exactly what segments exist to avoid.
4. **Kick the backfill** — spawn the detached summary process and return
   immediately.

`reconcile_one(config, path, *, event, trigger)` is the same pass narrowed to one
transcript: a hook says "this session just compacted / ended", and one extraction
plus one **forced** cut follows. It shares its commit phase with the full sweep
and kicks no backfill — the hook worker decides which sid to summarize.

The store/watermark mutation runs under the store-write lock; extraction runs
outside it.

## Hook ingest

Every hook on both harnesses pipes its stdin JSON into `sessionator ingest
--hook`. That path is short-circuited before argparse and is silent, total, and
sub-second: it spools the payload to `spool/<time_ns>-<pid>.json` atomically,
`Popen`s a detached `python -m sessionator _hook_worker <spool>`, and returns 0
whatever fails. Codex's `SessionEnd` is synchronous with a 3 s ceiling and Claude
Code reads a hook's stdout as protocol, so anything slower or chattier would be a
user-visible defect.

The worker then, out of band:

1. normalizes `hook_event_name` to the segment vocabulary (`PreCompact` →
   `precompact`, `SessionEnd` → `session_end`, …; anything else → `change`) and
   takes the trigger from `trigger` / `reason` / `source`;
2. resolves `transcript_path` through `adapters.adapter_for_path` and calls
   `reconcile_one`. **The transcript path is the session identity**, not the
   harness's `session_id` — Codex's is shared across forks. With no usable path,
   it falls back to the ordinary watermark-gated sweep;
3. runs `backfill(only_sid=…)` for each touched sid;
4. unlinks the spool file and reaps any older than 24 h.

A **non-blocking per-session lock** wraps steps 1–3, so the PreCompact/Stop storm
a busy session produces collapses into one worker instead of a pile-up. Every
exception is swallowed and the exit code is always 0.

## Summary execution — detached one-shot backfill

There is **no daemon**. The reconcile path (and each hook worker) writes the
deterministic record fast, then spawns a detached, short-lived backfill that
summarizes what is pending and exits. Queries never wait; a very fresh record may
briefly show as pending.

- **Segments first.** A segment is `{seq, event, trigger, start, end, bytes, at,
  state, summary}`, where `start`/`end` are half-open **turn ordinals** into the
  excerpt's turn sequence — not transcript byte offsets, because the summarizer
  reads the derived, privacy-stripped excerpt. Cuts are debounced (no new segment
  while one is pending) unless forced by a hook or by `summarize`. The work list
  is per-pending-segment, ordered by `(sid, seq)`, ahead of any legacy
  whole-record batch.
- **Roll-up, not re-summarize.** `build_rollup_prompt` feeds the model the
  *current* summary plus the *new* segment and asks for the merged five fields,
  so cost is proportional to what changed rather than to the session's length.
  Per-segment input comes from that segment's sidecar, falling back to a slice of
  the main excerpt.
- **Batching** (legacy whole-record path): up to ~8 sessions per CLI call, using
  the `@@S<n>@@` marker pattern to split one response back into per-session
  results.
- **Summarizer mapping**: `[summarize].prefer` decides which CLI runs, regardless
  of the session's own harness — default `claude` (`claude -p --model haiku`,
  effort medium), else `codex` (`codex exec`, `gpt-5.6-luna`, reasoning high), or
  `same` to use each session's own harness. The other CLI is the fallback. None
  installed → records stay deterministic-only and remain searchable.
- **Five fields + resolved**: the model returns `asked`, `learned`, `completed`,
  `left_off`, `next_steps`, and a refined `resolved` tri-state.
- **Failure**: a summarizer error marks that segment `error` (the record rolls up
  to `error` only when nothing is pending) and is retried on the next backfill. It
  never blocks or fails a query.
- **Self-pollution guard**: both prompts open with the sentinel line
  `@@SESSIONATOR-SUMMARIZER@@`, and the adapters filter any transcript whose first
  human turn carries it — otherwise summarizing would create sessions to
  summarize.

`sessionator summarize <sid-prefix>` is the manual path: it forces a segment over
everything not yet summarized and runs the backfill in the foreground, because a
user who asked for a summary is waiting for one.

Auth/stdin handling: the `claude -p` call unsets `ANTHROPIC_API_KEY` /
`ANTHROPIC_AUTH_TOKEN` (so it uses interactive session credentials) and passes
`--no-session-persistence` (so it does not self-pollute `~/.claude/projects`);
the `codex exec` call closes stdin (`stdin=DEVNULL`).

## Locking

One store-write lock guards the load → mutate → write critical section; reconcile
and the backfill's write-back take it blocking (with a timeout). Reads take no
lock (append-only JSONL, atomic rewrite). A separate single-instance
`backfill.lock` is taken non-blocking at backfill start, so a second backfill
exits at once. Hook workers take a third, per-session `hooks/<session>.lock`,
also non-blocking, so a compaction storm collapses to one worker.

## Config

XDG TOML at `$XDG_CONFIG_HOME/sessionator/config.toml` (`~/.config` fallback),
auto-written on first run: it auto-detects the CLIs and transcript dirs, writes
detected values plus shipped summarizer defaults, and prints what it found — no
wizard. Read with stdlib `tomllib`; written by a tiny hand-rolled emitter.
`status` displays the live config. Exclusion globs are edited here; the worked
example is Ed's local-only vault rule, `**/private-notes/**`.

Schema v2 adds `[summarize].prefer` and moves the Claude default to `haiku`. A v1
file still carrying the old shipped default (`opus-4.8`) is migrated on load and
rewritten; a model the user chose is left alone.

`sessionator setup codex` writes `$CODEX_HOME/hooks.json` separately from the skills plugin. It merges by command identity (including the
legacy unquoted form), rewrites its own handlers in place, appends new groups
last, and never touches Codex `config.toml`. New or changed definitions require
hook trust review. Claude capture is registered explicitly from
`plugin/claude-hooks/hooks.json`; Codex does not auto-discover that file.
