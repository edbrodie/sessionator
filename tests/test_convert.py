"""Tests for the legacy-store converter (convert.py)."""

from __future__ import annotations

import json

from sessionator.convert import (
    _codex_uuid,
    _legacy_key,
    convert,
    parse_summary,
)
from sessionator.store import Store

from conftest import make_config, make_record, seed_store


# ---------------------------------------------------------------------------
# summary parsing
# ---------------------------------------------------------------------------

def test_parse_summary_three_bullets():
    blob = (
        "- **Asked:** refactor the proposals for Chris\n"
        "- **Done:** Edited 6 files; ran team-context.\n"
        "- **Left off:** Uncommitted diff on the branch."
    )
    s = parse_summary(blob)
    assert s["asked"] == "refactor the proposals for Chris"
    assert s["completed"] == "Edited 6 files; ran team-context."
    assert s["left_off"] == "Uncommitted diff on the branch."
    assert s["learned"] == ""
    assert s["next_steps"] == ""


def test_parse_summary_left_off_hyphen_variant():
    s = parse_summary("- **Asked:** a\n- **Left-off:** b")
    assert s["asked"] == "a"
    assert s["left_off"] == "b"


def test_parse_summary_wrapped_lines_append():
    blob = "- **Asked:** first line\n  still the ask\n- **Done:** done it"
    s = parse_summary(blob)
    assert s["asked"] == "first line still the ask"
    assert s["completed"] == "done it"


def test_parse_summary_placeholder_is_empty():
    s = parse_summary("- _(summary unavailable)_")
    assert all(v == "" for v in s.values())


def test_parse_summary_none_and_blank():
    assert all(v == "" for v in parse_summary(None).values())
    assert all(v == "" for v in parse_summary("   ").values())


# ---------------------------------------------------------------------------
# key derivation
# ---------------------------------------------------------------------------

def test_codex_uuid_from_rollout_name():
    sid = "rollout-2026-07-13T21-43-34-019f5d38-8c93-7f42-8c83-2cc98d365537"
    assert _codex_uuid(sid) == "019f5d38-8c93-7f42-8c83-2cc98d365537"


def test_legacy_key_claude_and_codex():
    assert _legacy_key({"source": "claude", "session_id": "abc-123"}) == (
        "claude",
        "abc-123",
    )
    assert _legacy_key(
        {"source": "codex", "session_id": "rollout-2026-01-01T00-00-00-1-2-3-4-5"}
    ) == ("codex", "1-2-3-4-5")


def test_legacy_key_unknown_source_and_missing_id():
    assert _legacy_key({"source": "gemini", "session_id": "x"}) is None
    assert _legacy_key({"source": "claude", "session_id": ""}) is None


# ---------------------------------------------------------------------------
# end-to-end convert
# ---------------------------------------------------------------------------

def _write_legacy(tmp_path, rows):
    p = tmp_path / "sessions.jsonl"
    with open(p, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return p


def _old(source, session_id, **kw):
    d = {
        "source": source,
        "session_id": session_id,
        "date": "2026-05-01",
        "cwd": "/home/ed/proj",
        "last_active": "2026-05-01T10:00:00",
        "model": "gpt-5.6-sol" if source == "codex" else "opus-4.8",
        "repo": None,
        "branch": None,
        "files": [],
        "keywords": ["legacy-kw"],
        "commits": [],
        "prs": [],
        "skills": [],
        "subagents": [],
        "mcp": [],
        "resolved": "done",
        "open_todos": [],
        "tests": None,
        "summary": "- **Asked:** do the thing\n- **Done:** did it\n- **Left off:** nothing open",
        "transcript_path": "/old/path.jsonl",
    }
    d.update(kw)
    return d


def test_merge_grafts_summary_onto_pending_record(tmp_path):
    cfg = make_config(tmp_path)
    # A pending v1 record already ingested for the same claude session.
    rec = make_record(
        "claude/aaa-111",
        summary_state="pending",
        resolved="unknown",
        keywords=["new-kw"],
    )
    seed_store(cfg, [rec])

    legacy = _write_legacy(tmp_path, [_old("claude", "aaa-111")])
    res = convert(cfg, legacy)

    assert res.merged == 1
    assert res.appended == 0
    out = Store(cfg).load()["claude/aaa-111"]
    assert out.summary_state == "done"
    assert out.summary["asked"] == "do the thing"
    assert out.summary["completed"] == "did it"
    assert out.summary["left_off"] == "nothing open"
    assert out.resolved == "done"  # grafted from legacy
    assert "new-kw" in out.keywords and "legacy-kw" in out.keywords  # union


def test_merge_skips_record_with_real_summary(tmp_path):
    cfg = make_config(tmp_path)
    rec = make_record(
        "claude/bbb-222",
        summary_state="done",
        summary={"asked": "already summarized"},
    )
    seed_store(cfg, [rec])

    legacy = _write_legacy(tmp_path, [_old("claude", "bbb-222")])
    res = convert(cfg, legacy)

    assert res.skipped == 1
    assert res.merged == 0
    out = Store(cfg).load()["claude/bbb-222"]
    assert out.summary["asked"] == "already summarized"  # untouched


def test_append_when_no_v1_twin(tmp_path):
    cfg = make_config(tmp_path)
    seed_store(cfg, [])  # empty store

    legacy = _write_legacy(
        tmp_path,
        [_old("codex", "rollout-2026-05-01T10-00-00-11-22-33-44-55")],
    )
    res = convert(cfg, legacy)

    assert res.appended == 1
    out = Store(cfg).load()
    sid = "codex/11-22-33-44-55"
    assert sid in out
    rec = out[sid]
    assert rec.harness == "codex"
    assert rec.native_id == "11-22-33-44-55"
    assert rec.summary_state == "done"
    assert rec.summary["asked"] == "do the thing"
    assert rec.excerpt_path is None
    assert rec.transcript_path == "/old/path.jsonl"


def test_convert_is_idempotent(tmp_path):
    cfg = make_config(tmp_path)
    seed_store(cfg, [make_record("claude/ccc-333", summary_state="pending")])
    legacy = _write_legacy(
        tmp_path,
        [
            _old("claude", "ccc-333"),
            _old("codex", "rollout-2026-05-01T10-00-00-99-88-77-66-55"),
        ],
    )
    first = convert(cfg, legacy)
    assert first.merged == 1 and first.appended == 1

    snapshot = cfg.store_path.read_text()
    second = convert(cfg, legacy)
    # Second run grafts nothing new — both rows are now already-done.
    assert second.merged == 0 and second.appended == 0
    assert second.skipped == 2
    assert cfg.store_path.read_text() == snapshot  # byte-identical, true no-op


def test_dry_run_changes_nothing(tmp_path):
    cfg = make_config(tmp_path)
    seed_store(cfg, [make_record("claude/ddd-444", summary_state="pending")])
    legacy = _write_legacy(tmp_path, [_old("claude", "ddd-444")])
    before = cfg.store_path.read_text()

    res = convert(cfg, legacy, dry_run=True)
    assert res.merged == 1
    assert cfg.store_path.read_text() == before  # unchanged


def test_excluded_cwd_dropped(tmp_path):
    cfg = make_config(tmp_path, exclusions=["**/private-notes/**"])
    seed_store(cfg, [])
    legacy = _write_legacy(
        tmp_path,
        [_old("claude", "eee-555", cwd="/home/ed/private-notes/vault")],
    )
    res = convert(cfg, legacy)
    assert res.excluded == 1
    assert res.appended == 0
    assert "claude/eee-555" not in Store(cfg).load()


def test_tombstoned_sid_not_appended(tmp_path):
    cfg = make_config(tmp_path)
    store = seed_store(cfg, [])
    store.write_tombstones({"claude/fff-666"})
    legacy = _write_legacy(tmp_path, [_old("claude", "fff-666")])
    res = convert(cfg, legacy)
    assert res.tombstoned == 1
    assert res.appended == 0
    assert "claude/fff-666" not in Store(cfg).load()


def test_unknown_source_and_parse_errors_counted(tmp_path):
    cfg = make_config(tmp_path)
    seed_store(cfg, [])
    p = tmp_path / "sessions.jsonl"
    with open(p, "w") as f:
        f.write(json.dumps(_old("gemini", "zzz")) + "\n")
        f.write("{ not json\n")
    res = convert(cfg, p)
    assert res.unknown_source == 1
    assert res.parse_errors == 1


def test_private_span_stripped_from_grafted_summary(tmp_path):
    cfg = make_config(tmp_path)
    seed_store(cfg, [make_record("claude/ggg-777", summary_state="pending")])
    row = _old(
        "claude",
        "ggg-777",
        summary="- **Asked:** handle <private>secret token</private> safely\n- **Done:** done",
    )
    legacy = _write_legacy(tmp_path, [row])
    convert(cfg, legacy)
    out = Store(cfg).load()["claude/ggg-777"]
    assert "secret token" not in out.summary["asked"]
    assert "[private]" in out.summary["asked"]


def test_missing_source_file_raises(tmp_path):
    cfg = make_config(tmp_path)
    seed_store(cfg, [])
    try:
        convert(cfg, tmp_path / "does-not-exist.jsonl")
    except FileNotFoundError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected FileNotFoundError")
