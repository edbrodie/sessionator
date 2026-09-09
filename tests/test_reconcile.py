import json

from sessionator.reconcile import reconcile
from sessionator.store import Store
from conftest import make_config, make_record, seed_store

CLAUDE_SID = "claude/4f00dc4e-0d36-4bd9-a481-39ef82c19509"
CODEX_SID = "codex/019f5d38-8c93-7f42-8c83-2cc98d365537"

VAULT_GLOB = "**/private-notes/**"
VAULT_PATH = "/Users/ed/Desktop/private-notes/secret.md"


def test_reconcile_ingests_both_harnesses(tmp_path, claude_fixtures, codex_fixtures):
    cfg = make_config(tmp_path, claude_dir=claude_fixtures, codex_dir=codex_fixtures)
    result = reconcile(cfg, kick_backfill=False)

    store = Store(cfg)
    records = store.load()
    assert CLAUDE_SID in records
    assert CODEX_SID in records
    # Sidechain + headless are filtered, not stored.
    assert result.upserted_new == 2
    assert result.by_harness == {"claude": 1, "codex": 1}
    # Every stored record starts pending its summary.
    assert all(r.summary_state == "pending" for r in records.values())


def test_reconcile_writes_excerpt_sidecar_without_private(tmp_path, claude_fixtures):
    cfg = make_config(tmp_path, claude_dir=claude_fixtures)
    reconcile(cfg, kick_backfill=False)
    store = Store(cfg)
    rec = store.load()[CLAUDE_SID]
    assert rec.excerpt_path
    body = store.read_excerpt(rec)
    assert body
    assert "sk-secret-abc123" not in body
    assert "[private]" in body


def test_reconcile_watermark_idempotent(tmp_path, claude_fixtures, codex_fixtures):
    cfg = make_config(tmp_path, claude_dir=claude_fixtures, codex_dir=codex_fixtures)
    reconcile(cfg, kick_backfill=False)
    second = reconcile(cfg, kick_backfill=False)
    # Nothing changed on disk -> no re-extraction.
    assert second.extracted == 0
    assert second.upserted_new == 0


def test_reconcile_exclusion_skips_ingest(tmp_path, claude_fixtures, codex_fixtures):
    cfg = make_config(
        tmp_path, claude_dir=claude_fixtures, codex_dir=codex_fixtures,
        exclusions=["**/proj"],
    )
    result = reconcile(cfg, kick_backfill=False)
    store = Store(cfg)
    assert store.load() == {}
    assert result.excluded == 2


def test_reconcile_retroactive_purge(tmp_path, claude_fixtures, codex_fixtures):
    cfg = make_config(tmp_path, claude_dir=claude_fixtures, codex_dir=codex_fixtures)
    reconcile(cfg, kick_backfill=False)
    store = Store(cfg)
    assert len(store.load()) == 2

    # Add an exclusion and reconcile again: matching records are purged even
    # though the transcripts are unchanged (watermark would otherwise skip them).
    cfg.exclusions = ["**/proj"]
    result = reconcile(cfg, kick_backfill=False)
    assert store.load() == {}
    assert result.purged == 2


def test_reconcile_respects_tombstone(tmp_path, claude_fixtures):
    cfg = make_config(tmp_path, claude_dir=claude_fixtures)
    store = Store(cfg)
    store.write_tombstones([CLAUDE_SID])
    result = reconcile(cfg, kick_backfill=False)
    assert CLAUDE_SID not in store.load()
    assert result.tombstoned_skipped >= 1


def test_reconcile_scrubs_stored_files_on_glob_change(tmp_path):
    # A kept (non-excluded cwd) record carrying a vault path in its files
    # manifest gets that entry redacted when the exclusion set changes.
    cfg = make_config(tmp_path)
    rec = make_record(
        "claude/vault1",
        cwd="/home/ed/proj",
        files=[["C", VAULT_PATH], ["M", "src/a.py"]],
    )
    seed_store(cfg, [rec])

    cfg.exclusions = [VAULT_GLOB]
    result = reconcile(cfg, kick_backfill=False)
    out = Store(cfg).load()["claude/vault1"]
    assert ["scrubbed", "[excluded-path]"] in out.files
    assert ["M", "src/a.py"] in out.files
    assert "private-notes" not in repr(out.files)
    assert result.scrubbed == 1

    # Globs unchanged next run -> no re-scan, files stay clean.
    result2 = reconcile(cfg, kick_backfill=False)
    assert result2.scrubbed == 0
    assert Store(cfg).load()["claude/vault1"].files == out.files


def test_reconcile_scrubs_stored_excerpt_on_glob_change(tmp_path):
    cfg = make_config(tmp_path)
    store = Store(cfg)
    rec = make_record("claude/vault2", cwd="/home/ed/proj")
    rec.excerpt = f"USER: please edit {VAULT_PATH} then run tests"
    rec.excerpt_path = store.write_excerpt(rec)
    store.write({rec.sid: rec})

    cfg.exclusions = [VAULT_GLOB]
    result = reconcile(cfg, kick_backfill=False)
    body = Store(cfg).read_excerpt(Store(cfg).load()["claude/vault2"])
    assert "private-notes" not in body
    assert "[excluded-path]" in body
    assert result.scrubbed == 1


def test_reconcile_scrubs_new_record_at_ingest(tmp_path):
    # An excluded path present in a freshly extracted (kept) session is scrubbed
    # before the record is ever written — both files manifest and excerpt.
    cdir = tmp_path / "claude" / "-work-proj"
    cdir.mkdir(parents=True)
    uuid = "11111111-2222-3333-4444-555555555555"
    lines = [
        {
            "type": "user",
            "cwd": "/work/proj",
            "timestamp": "2026-07-01T10:00:00Z",
            "message": {"role": "user", "content": "do the thing"},
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "opus",
                "content": [
                    {"type": "text", "text": f"writing {VAULT_PATH}"},
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Write",
                        "input": {"file_path": VAULT_PATH},
                    },
                ],
            },
        },
    ]
    (cdir / f"{uuid}.jsonl").write_text("\n".join(json.dumps(x) for x in lines))

    cfg = make_config(
        tmp_path, claude_dir=tmp_path / "claude", exclusions=[VAULT_GLOB]
    )
    result = reconcile(cfg, kick_backfill=False)
    rec = Store(cfg).load()[f"claude/{uuid}"]
    assert result.scrubbed == 1
    assert ["scrubbed", "[excluded-path]"] in rec.files
    assert "private-notes" not in repr(rec.files)
    body = Store(cfg).read_excerpt(rec)
    assert "private-notes" not in body
    assert "[excluded-path]" in body


# --- segments: growth, debounce, hook path ---------------------------------

from sessionator import segments as seg  # noqa: E402
from sessionator.reconcile import reconcile_one  # noqa: E402


def _claude_lines(n_turns, day="2026-07-01"):
    lines = []
    for i in range(n_turns):
        lines.append({
            "type": "user",
            "cwd": "/work/proj",
            "timestamp": f"{day}T10:0{i}:00Z",
            "message": {"role": "user", "content": f"ask number {i}"},
        })
        lines.append({
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "haiku",
                "content": [{"type": "text", "text": f"answer number {i}"}],
            },
        })
    return lines


def _write_claude(tmp_path, uuid, lines):
    d = tmp_path / "claude" / "-work-proj"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{uuid}.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return p


UUID = "aaaa0001-0000-4000-8000-000000000001"
SID = f"claude/{UUID}"


def _grow_and_reconcile(cfg, path, lines):
    with open(path, "a") as f:
        for x in lines:
            f.write(json.dumps(x) + "\n")
    return reconcile(cfg, kick_backfill=False)


def test_plain_ingest_opens_no_segments(tmp_path):
    path = _write_claude(tmp_path, UUID, _claude_lines(2))
    cfg = make_config(tmp_path, claude_dir=tmp_path / "claude")
    reconcile(cfg, kick_backfill=False)
    rec = Store(cfg).load()[SID]
    assert rec.summary_segments == []
    assert rec.summary_state == "pending"
    assert path.exists()


def test_growth_on_segmented_record_cuts_one_debounced_segment(tmp_path):
    path = _write_claude(tmp_path, UUID, _claude_lines(2))
    cfg = make_config(tmp_path, claude_dir=tmp_path / "claude")
    reconcile(cfg, kick_backfill=False)
    store = Store(cfg)

    # Pretend a hook already cut and summarized everything so far.
    records = store.load()
    rec = records[SID]
    seg.append_segment(
        rec, event="precompact", trigger="auto",
        turn_count=seg.count_turns(store.read_excerpt(rec)),
        size=path.stat().st_size,
    )
    seg.mark_done(rec, 1, {"asked": "earlier work"})
    rec.summary_state = "done"
    store.write(records)
    first_end = rec.summary_segments[0]["end"]

    # Growth cuts exactly the new turns and shows as partial.
    _grow_and_reconcile(cfg, path, _claude_lines(1, day="2026-07-02"))
    rec = store.load()[SID]
    assert len(rec.summary_segments) == 2
    new = rec.summary_segments[1]
    assert new["state"] == "pending"
    assert new["event"] == "change"
    assert new["start"] == first_end and new["end"] > first_end
    assert rec.summary_state == "partial"
    # The slice was written to its own sidecar at cut time.
    assert "ask number 0" in store.read_segment_excerpt(rec, 2)

    # A second growth is debounced while that segment is still pending.
    _grow_and_reconcile(cfg, path, _claude_lines(1, day="2026-07-03"))
    rec = store.load()[SID]
    assert len(rec.summary_segments) == 2
    assert rec.summary_state == "partial"


def test_growth_on_legacy_record_still_marks_stale(tmp_path):
    path = _write_claude(tmp_path, UUID, _claude_lines(2))
    cfg = make_config(tmp_path, claude_dir=tmp_path / "claude")
    reconcile(cfg, kick_backfill=False)
    store = Store(cfg)
    records = store.load()
    records[SID].summary_state = "done"
    store.write(records)

    _grow_and_reconcile(cfg, path, _claude_lines(1, day="2026-07-02"))
    rec = store.load()[SID]
    assert rec.summary_segments == []
    assert rec.summary_state == "stale"


def test_reconcile_one_touches_a_single_sid_and_forces_a_cut(tmp_path):
    path = _write_claude(tmp_path, UUID, _claude_lines(2))
    _write_claude(tmp_path, "bbbb0002-0000-4000-8000-000000000002", _claude_lines(1))
    cfg = make_config(tmp_path, claude_dir=tmp_path / "claude")

    result = reconcile_one(cfg, path, sid=SID, event="session_end", trigger="exit")
    assert result.touched_sids == [SID]
    assert result.extracted == 1

    store = Store(cfg)
    records = store.load()
    assert set(records) == {SID}  # the other transcript was never read
    rec = records[SID]
    assert len(rec.summary_segments) == 1
    segment = rec.summary_segments[0]
    assert (segment["event"], segment["trigger"], segment["start"]) == (
        "session_end", "exit", 0,
    )
    assert segment["bytes"] == path.stat().st_size
    assert rec.summary_state == "pending"
    assert store.read_segment_excerpt(rec, 1)

    # The watermark was recorded, so a later full sweep does not re-extract it.
    assert reconcile(cfg, kick_backfill=False).extracted == 1  # only the other one

    # A second hook on the same session forces a cut past the debounce.
    reconcile_one(cfg, path, sid=SID, event="stop", trigger=None)
    rec = Store(cfg).load()[SID]
    assert [s["event"] for s in rec.summary_segments] == ["session_end", "stop"]


def test_reconcile_one_ignores_unreadable_and_filtered_paths(tmp_path):
    cfg = make_config(tmp_path, claude_dir=tmp_path / "claude")
    missing = reconcile_one(cfg, tmp_path / "nope.jsonl", event="session_end")
    assert missing.extracted == 0 and missing.touched_sids == []


def test_codex_compacted_line_segments_an_unhooked_session(tmp_path):
    d = tmp_path / "codex" / "2026" / "07" / "12"
    d.mkdir(parents=True)
    uuid = "019f8888-0000-7000-8000-00000000000c"
    lines = [
        {"type": "session_meta", "payload": {
            "id": uuid, "originator": "codex-tui", "thread_source": "user",
            "cwd": "/work/proj"}},
        {"type": "event_msg", "timestamp": "2026-07-12T09:00:00Z",
         "payload": {"type": "user_message", "message": "start the work"}},
        {"type": "event_msg", "payload": {"type": "agent_message", "message": "ok"}},
        {"type": "compacted"},
        {"type": "event_msg", "timestamp": "2026-07-12T10:00:00Z",
         "payload": {"type": "user_message", "message": "keep going"}},
        {"type": "event_msg", "payload": {"type": "agent_message", "message": "done"}},
    ]
    (d / f"rollout-2026-07-12T09-00-00-{uuid}.jsonl").write_text(
        "\n".join(json.dumps(x) for x in lines) + "\n"
    )
    cfg = make_config(tmp_path, codex_dir=tmp_path / "codex")
    reconcile(cfg, kick_backfill=False)
    rec = Store(cfg).load()[f"codex/{uuid}"]
    # The compaction marker plus the tail after it.
    assert [(s["event"], s["start"], s["end"]) for s in rec.summary_segments] == [
        ("precompact", 0, 2), ("change", 2, 4),
    ]
    assert rec.summary_state == "pending"
