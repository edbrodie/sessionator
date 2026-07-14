"""Index build, rebuild-on-mismatch, feature detection, incremental sync."""

from __future__ import annotations

import sqlite3

from sessionator.index import USER_VERSION, Index

from conftest import make_config, make_record, seed_store


def _cfg(tmp_path):
    return make_config(tmp_path)


def test_build_creates_schema_and_rows(tmp_path):
    cfg = _cfg(tmp_path)
    recs = {
        "claude/aaaa": make_record("claude/aaaa", summary={"asked": "wire the router"}),
        "codex/bbbb": make_record("codex/bbbb", summary={"asked": "trace the bug"}),
    }
    seed_store(cfg, recs.values())

    idx = Index(cfg).ensure_current(recs)
    conn = idx.connect()
    assert idx.has_fts is True
    assert conn.execute("PRAGMA user_version").fetchone()[0] == USER_VERSION
    assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM fts").fetchone()[0] == 2
    idx.close()


def test_feature_detection_reports_fts_and_trigram(tmp_path):
    # This SQLite build ships both; the flags must reflect that.
    idx = Index(_cfg(tmp_path))
    idx.connect()
    assert idx.has_fts is True
    assert idx.has_tri is True
    idx.close()


def test_rebuild_on_version_mismatch(tmp_path):
    cfg = _cfg(tmp_path)
    recs = {"claude/aaaa": make_record("claude/aaaa", summary={"asked": "alpha"})}
    seed_store(cfg, recs.values())
    Index(cfg).ensure_current(recs).close()

    # Simulate a schema from a future/old version.
    conn = sqlite3.connect(str(cfg.index_path))
    conn.execute("PRAGMA user_version = 999")
    conn.commit()
    conn.close()

    idx2 = Index(cfg)
    conn2 = idx2.connect()  # connect() rebuilds on mismatch
    assert conn2.execute("PRAGMA user_version").fetchone()[0] == USER_VERSION
    idx2.ensure_current(recs)  # watermark gone after rebuild → re-syncs
    assert conn2.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1
    idx2.close()


def test_rebuild_on_missing_table(tmp_path):
    cfg = _cfg(tmp_path)
    recs = {"claude/aaaa": make_record("claude/aaaa", summary={"asked": "alpha"})}
    seed_store(cfg, recs.values())
    Index(cfg).ensure_current(recs).close()

    conn = sqlite3.connect(str(cfg.index_path))
    conn.execute("DROP TABLE fts")
    conn.commit()
    conn.close()

    idx2 = Index(cfg)
    idx2.connect()  # missing fts → full rebuild
    idx2.ensure_current(recs)
    porter, _tri = idx2.fts_rank_lists(["alpha"])
    assert porter == ["claude/aaaa"]
    idx2.close()


def test_incremental_add_change_delete(tmp_path):
    cfg = _cfg(tmp_path)
    r1 = make_record("claude/aaaa", summary={"asked": "alpha"})
    r2 = make_record("codex/bbbb", summary={"asked": "beta"})
    seed_store(cfg, [r1, r2])
    Index(cfg).ensure_current({r1.sid: r1, r2.sid: r2}).close()

    # Add a third, change r1's content (new indexed_at), drop r2.
    r1b = make_record(
        "claude/aaaa", summary={"asked": "gamma"}, indexed_at="2026-07-02T00:00:00"
    )
    r3 = make_record("claude/cccc", summary={"asked": "delta"})
    recs2 = {r1b.sid: r1b, r3.sid: r3}
    seed_store(cfg, recs2.values())

    idx = Index(cfg).ensure_current(recs2)
    conn = idx.connect()
    assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 2
    # r1 now matches "gamma", not "alpha"; r2 gone; r3 present.
    assert idx.fts_rank_lists(["gamma"])[0] == ["claude/aaaa"]
    assert idx.fts_rank_lists(["alpha"])[0] == []
    assert idx.fts_rank_lists(["beta"])[0] == []
    assert idx.fts_rank_lists(["delta"])[0] == ["claude/cccc"]
    idx.close()


def test_watermark_gate_skips_unchanged_store(tmp_path):
    cfg = _cfg(tmp_path)
    r1 = make_record("claude/aaaa", summary={"asked": "alpha"})
    recs = {r1.sid: r1}
    seed_store(cfg, [r1])
    Index(cfg).ensure_current(recs).close()

    # Tamper with the index out-of-band, then re-run ensure_current. Because the
    # store watermark is unchanged, the sync is skipped and the tamper survives —
    # proving the gate short-circuits rather than re-syncing every call.
    conn = sqlite3.connect(str(cfg.index_path))
    conn.execute("DELETE FROM fts")
    conn.commit()
    conn.close()

    idx = Index(cfg).ensure_current(recs)
    assert idx.fts_rank_lists(["alpha"])[0] == []  # not restored → gate held
    idx.close()
