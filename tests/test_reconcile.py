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

    # Growth past the floor cuts exactly the new turns and shows as partial.
    _grow_and_reconcile(cfg, path, _claude_lines(2, day="2026-07-02"))
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


def test_small_growth_on_segmented_record_waits_for_the_floor(tmp_path):
    """A reconcile runs before every query; a couple of new turns on a live
    session must not open a segment (and a summarizer call) each time."""
    path = _write_claude(tmp_path, UUID, _claude_lines(2))
    cfg = make_config(tmp_path, claude_dir=tmp_path / "claude")
    reconcile(cfg, kick_backfill=False)
    store = Store(cfg)
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

    # One exchange (2 turns) is below MIN_CHANGE_TURNS: no cut, still done.
    _grow_and_reconcile(cfg, path, _claude_lines(1, day="2026-07-02"))
    rec = store.load()[SID]
    assert len(rec.summary_segments) == 1
    assert rec.summary_state == "done"

    # Once enough has accumulated, the cut covers everything since the last end.
    _grow_and_reconcile(cfg, path, _claude_lines(1, day="2026-07-03"))
    rec = store.load()[SID]
    assert len(rec.summary_segments) == 2
    assert rec.summary_segments[1]["end"] - rec.summary_segments[1]["start"] >= seg.MIN_CHANGE_TURNS
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


# --- watermarks v2: identity, not path -------------------------------------

import json as _json  # noqa: E402
import shutil  # noqa: E402

from sessionator.store import WATERMARK_SCHEMA  # noqa: E402

DESKTOP_UUID = "019fd35c-0000-7000-8000-00000000000d"
DESKTOP_NAME = f"rollout-2026-07-12T14-00-00-{DESKTOP_UUID}.jsonl"


def _codex_home(tmp_path, codex_desktop_fixtures):
    """A codex home with one live desktop rollout and an empty archive dir."""
    home = tmp_path / "codex-home"
    live = home / "sessions" / "2026" / "07" / "12"
    live.mkdir(parents=True)
    (home / "archived_sessions").mkdir()
    shutil.copy(
        codex_desktop_fixtures / "2026" / "07" / "12" / DESKTOP_NAME,
        live / DESKTOP_NAME,
    )
    return home, make_config(tmp_path, codex_dir=home / "sessions")


def test_archiving_a_rollout_does_not_re_extract_it(tmp_path, codex_desktop_fixtures):
    home, cfg = _codex_home(tmp_path, codex_desktop_fixtures)
    sid = f"codex/{DESKTOP_UUID}"

    first = reconcile(cfg, kick_backfill=False)
    assert first.upserted_new == 1
    rec = Store(cfg).load()[sid]
    assert rec.client == "Codex Desktop"

    # Archiving in the desktop app MOVES the rollout into a flat dir: new path,
    # new mtime, identical bytes. The watermark is keyed on the rollout uuid and
    # compares size, so this is a no-op rather than a full re-extraction.
    src = home / "sessions" / "2026" / "07" / "12" / DESKTOP_NAME
    dst = home / "archived_sessions" / DESKTOP_NAME
    shutil.move(str(src), str(dst))

    second = reconcile(cfg, kick_backfill=False)
    assert second.scanned == 1          # found in the archive root
    assert second.extracted == 0        # but recognized as already ingested
    assert second.upserted_new == 0 and second.upserted_changed == 0

    after = Store(cfg).load()
    assert set(after) == {sid}
    assert after[sid].summary_segments == []   # no spurious segment cut


def test_archived_only_rollout_is_still_ingested(tmp_path, codex_desktop_fixtures):
    # A session archived before sessionator ever saw it must still be captured.
    home, cfg = _codex_home(tmp_path, codex_desktop_fixtures)
    shutil.move(
        str(home / "sessions" / "2026" / "07" / "12" / DESKTOP_NAME),
        str(home / "archived_sessions" / DESKTOP_NAME),
    )
    reconcile(cfg, kick_backfill=False)
    assert f"codex/{DESKTOP_UUID}" in Store(cfg).load()


def test_v1_watermarks_migrate_to_v2_keys(tmp_path, claude_fixtures):
    cfg = make_config(tmp_path, claude_dir=claude_fixtures)
    store = Store(cfg)
    path = (
        claude_fixtures / "-home-u-proj"
        / "4f00dc4e-0d36-4bd9-a481-39ef82c19509.jsonl"
    )
    st = path.stat()

    # A schema-1 file: a bare dict keyed on the transcript path, no wrapper.
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    store.watermarks_path.write_text(_json.dumps({str(path): [st.st_mtime, st.st_size]}))

    migrated = store.load_watermarks()
    assert migrated == {f"claude:{path.stem}": [st.st_mtime, st.st_size]}

    # The migrated watermark is honoured, so nothing is re-extracted...
    result = reconcile(cfg, kick_backfill=False)
    assert CLAUDE_SID not in Store(cfg).load()
    assert result.extracted == 0

    # ...and the file is rewritten in v2 shape.
    on_disk = _json.loads(store.watermarks_path.read_text())
    assert on_disk["schema"] == WATERMARK_SCHEMA
    assert f"claude:{path.stem}" in on_disk["entries"]


def test_unrecognized_watermark_paths_keep_a_path_key(tmp_path):
    cfg = make_config(tmp_path)
    store = Store(cfg)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    store.watermarks_path.write_text(_json.dumps({"/tmp/mystery.log": [1.0, 2]}))
    assert store.load_watermarks() == {"path:/tmp/mystery.log": [1.0, 2]}


def test_corrupt_watermarks_file_is_ignored(tmp_path):
    cfg = make_config(tmp_path)
    store = Store(cfg)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    store.watermarks_path.write_text("[1, 2, 3]")
    assert store.load_watermarks() == {}
    store.watermarks_path.write_text('{"schema": 2, "entries": "nope"}')
    assert store.load_watermarks() == {}


def test_touching_a_transcript_does_not_re_extract_it(tmp_path):
    import os

    path = _write_claude(tmp_path, UUID, _claude_lines(2))
    cfg = make_config(tmp_path, claude_dir=tmp_path / "claude")
    reconcile(cfg, kick_backfill=False)

    os.utime(path, (0, 0))   # mtime changed, bytes identical
    assert reconcile(cfg, kick_backfill=False).extracted == 0
