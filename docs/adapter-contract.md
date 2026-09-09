# Adapter contract & defensive parsing

An **adapter** teaches sessionator how to read one coding-agent harness's
transcripts and turn them into the common schema-v1 record. Claude Code and
Codex ship in v1; a third harness is a new module plus one registry entry, with
no change to the core.

## The four-function API (plus one optional fifth)

Each adapter is a module registered in `sessionator.adapters.ADAPTERS`
(`name -> module`). It exposes:

- **`NAME: str`** — the harness key, e.g. `"claude"` / `"codex"`. It is the
  first half of every sid this adapter produces.
- **`discover_sources(config) -> list[Path]`** — the transcript root(s) to scan.
  Claude: `$CLAUDE_CONFIG_DIR/projects` else `~/.claude/projects`. Codex:
  `$CODEX_HOME/sessions` else `~/.codex/sessions`, **plus its
  `archived_sessions/` sibling when present** — archiving a Codex thread *moves*
  the rollout there, so a single-root adapter loses the session. A missing
  directory yields an empty list — never an error.
- **`enumerate_sessions(root) -> Iterator[(path, mtime, size)]`** — the candidate
  transcript files under a root, with the stat used for watermark comparison. No
  parsing happens here; cheap filesystem enumeration only.
- **`extract(path, config) -> Record | None`** — parse one transcript into a
  schema-v1 `Record`, deterministically. `None` means "not an interactive
  session" (filtered). `extract` is **pure**: no network, no LLM, no writes. It
  sets `client` to the harness build that wrote the transcript (`claude-code`;
  the Codex `originator`, e.g. `Codex Desktop`), or leaves it null.
- **`watermark_key(path) -> str`** *(optional)* — a path-independent identity for
  one transcript, used as the watermark key instead of the path. Claude returns
  `claude:<filename stem>`, Codex `codex:<rollout uuid>`. Declare it when your
  harness can move a transcript without changing its content; without it the core
  falls back to `path:<path>`, and a move reads as a brand-new session.

The record's transient `excerpt` field carries the capped, `<private>`-stripped
USER/ASSISTANT text; the store writes it to a sidecar and clears it from the
persisted record.

## Session key (sid)

`sid = "<harness>/<uuid>"` — e.g. `claude/b4ae266f-…`, `codex/019f5d38-…`.

- **Claude**: the uuid is the transcript filename stem, which is also the
  `--resume` id. `native_id` = that uuid.
- **Codex**: the uuid is the rollout's **top-level `id`** (from
  `session_meta.payload.id`) — fork-unique. It is **not** `session_id`, which is
  shared across forks. `native_id` = that id (the `codex resume <id>` argument);
  `forked_from` captures `forked_from_id` / `parent_thread_id` lineage.

## Defensive parsing — normative rules

1. **Malformed lines**: skip and increment the record's `parse_warnings`; never
   raise.
2. **Unknown message/event types**: ignore. Claude's transcript format is
   documented as unstable, so skip-unknown is mandatory, not best-effort.
3. **Claude filters**: skip `/subagents/` subtrees and `isSidechain` sessions;
   skip sessionator's own summarizer transcripts (first genuine human turn
   matches the batch-prompt signature, incl. a leading `@@S1@@`); never capture a
   synthetic (`<…>`-prefixed) model id.
4. **Codex filters**: interactive top-level rollouts only — line 1 must be a
   `session_meta` with `thread_source == "user"` and an `originator` that is not
   on the **denylist** `{codex-exec, codex-subagent, codex-mcp, codex-cloud,
   codex-automation}`, compared after folding whitespace and underscores to
   hyphens and lowercasing (so `codex_exec` and `Codex Exec` are the same entry);
   a missing or empty originator is also rejected. This excludes headless `codex
   exec` (and so sessionator's own summarizer calls), subagents, and
   machine-driven threads, while admitting every human-driven client — the
   desktop app writes `Codex Desktop`, the TUI writes `codex-tui`, and the next
   client will write something nobody has seen yet. **The filter must stay a
   denylist**: an allowlist silently dropped every desktop session, which is ~99%
   of real Codex usage. `history.jsonl` is never read. Both `sessions/` and
   `archived_sessions/` are scanned, and the watermark is keyed on the rollout
   uuid, so archiving is a move rather than a re-ingest.
5. **Minimal required fields**: sid, harness, date. Everything else is
   best-effort nullable — a record with gaps beats a dropped session.
6. **Compaction / pruning**: extract from what is on disk now. Upsert-on-change
   (see the index design) makes last-write-win the semantics; the excerpt
   sidecar preserves a pre-pruning view. When a transcript records *where* it was
   compacted — Codex writes a `{"type": "compacted"}` line — call
   `Walker.mark_boundary("precompact", "auto")` at that point. The reconcile
   replays those boundaries as summary segments, so a session no hook ever
   observed is still summarized in the pieces it was actually lived in, rather
   than as one pass over what survived. A boundary before the first turn is
   dropped, and consecutive boundaries on the same turn collapse.

## Porting note

The deterministic extraction (keywords, commits, PRs, repo/branch, files, tests,
resolved, excerpt) is the proven walker from `session_report_lib.py`, reshaped
behind this API as the shared `adapters._common.Walker`. The two adapters only
translate their transcript shape into `Walker` calls. All report/markdown
rendering and the old store shape are dropped; summarization moved to the
detached backfill.
