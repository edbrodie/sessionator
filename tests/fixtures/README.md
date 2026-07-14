# Test fixture corpus

Every file here is **synthetic** — hand-specified, never derived from a real
transcript. Private-looking tokens (`sk-fake-…`, `tok-fake-…`, `SENTINEL-…`) are
fabricated placeholders. The corpus was emitted once by a generator and is
committed as static data.

Three groups:

| Group | Dirs | Scanned by | Purpose |
|-------|------|-----------|---------|
| Adapter smoke | `claude/`, `codex/`, `codex_headless/` | `test_adapters.py`, `test_reconcile.py` | Happy-path extraction + the two filtered cases; **record counts are pinned** — do not add ingestable sessions here. |
| Defensive | `defensive/` | `test_defensive.py` | One fixture per T-007 rule gap. Kept out of the smoke dirs so the reconcile counts stay stable. |
| E2E / golden | `e2e/` | `test_golden.py`, CI e2e step | Real harness dir layout (`*-home/{projects,sessions}`) with stable far-past dates for deterministic golden output. |

## `defensive/` — T-007 rule → fixture

Rules are the ones in `docs/adapter-contract.md` §"Defensive parsing".

### Claude (`defensive/claude/-home-u-proj/`, unless noted)

| Fixture | Rule | What it exercises |
|---------|------|-------------------|
| `d1malform-…01.jsonl` | 1 | A malformed line between two good turns → `parse_warnings == 1`, record survives. |
| `d2unknown-…02.jsonl` | 2 | `summary` / `file-history-snapshot` / `system` / a never-seen type interleaved → ignored, not counted as warnings. |
| `d3summ-…03.jsonl` | 3 | First human turn is a `@@S1@@` summarizer batch prompt → filtered (`None`). |
| `d3synth-…04.jsonl` | 3 | A `<synthetic>` API-retry model id is ignored; the real model wins. |
| `subagents/d3subagent-…09.jsonl` | 3 | Lives under a `/subagents/` subtree → skipped by `enumerate_sessions`. |
| `../-home-u-nocwd/d5nocwd-…05.jsonl` | 5 | No inline `cwd` anywhere → decoded from the mangled folder name (`/home/u/nocwd`). |
| `d5nomodel-…06.jsonl` | 5 | Assistant turn has no `model` → `model is None`, record still produced. |
| `d5nots-…07.jsonl` | 5 | No timestamps → `date` falls back to the file mtime day. |
| `d6precompact-…08.jsonl` | 6 | Opens mid-stream with a PreCompact summary record → still extracts from what's on disk. |

### Codex (`defensive/codex/2026/07/11/`)

| Fixture (by uuid) | Rule | What it exercises |
|-------------------|------|-------------------|
| `…019f7001…` | 1 | Malformed body line → `parse_warnings == 1`, record survives. |
| `…019f7002…` | 2 | Unknown `response_item` / `event_msg` payload types + an unknown top-level type → ignored. |
| `…019f7003…` | 4 | `forked_from_id` captured; sid keyed on the fork-unique top-level `id`, not the shared `session_id`. |
| `…019f7004…` | 4 | `thread_source != user` → filtered. |
| `…019f7005…` | 4 | `originator != codex-tui` (nested subagent) → filtered. |
| `…019f7006…` | 5 | No `turn_context` → `model is None`, record still produced. |
| `…019f7007…` | 5 | No `cwd` in `session_meta` → `cwd == ""` (no folder fallback for codex). |

### Codex both-sources (`defensive/codex_both/`)

`sessions/2026/07/11/…019f7008….jsonl` is a normal rollout; `history.jsonl` sits
beside `sessions/` carrying a sentinel. Proves rule 4's "`history.jsonl` is never
read": enumerate yields only the rollout, and the sentinel never reaches a record.

(`codex_headless/` — the headless `codex exec` filter, `originator == codex_exec`
— lives at the top level and is asserted by `test_adapters.py`.)

## `e2e/` — golden corpus

Layout mirrors a real machine so `CLAUDE_CONFIG_DIR` / `CODEX_HOME` can point
straight at the `*-home` roots (their `projects/` and `sessions/` children are the
transcript dirs):

| Session | Date | Notes |
|---------|------|-------|
| `claude/c1a0d001…` | 2020-03-15 | auth fix; commit `ab12345`, PR `/42`, `pytest` 3 passed, resolved `done`. |
| `codex/019fc0de…` | 2020-02-10 | parser refactor; `forked_from`, commit `cd67890`, PR `/43`, resolved `done`. |
| `claude/c1a0d002…` | 2020-01-05 | dashboard panel; one `Write`, resolved `unknown`. |

Dates are fixed in the far past so recency ordering and every rendered date are
deterministic no matter when the suite runs. The two claude uuids share the
`c1a0d00…` prefix on purpose, to exercise ambiguous-prefix resolution (`show`
exit 2). The CI "fixture-store e2e" step ingests this corpus with the real
`sessionator` binary and asserts search/show/resume output + grep exit codes.
