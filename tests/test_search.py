"""Search ranking (RRF + recency decay), filters, and the linear fallback."""

from __future__ import annotations

from datetime import date

import sessionator.index as index_mod
from sessionator.search import Filters, search

from conftest import make_config, make_record, seed_store

TODAY = date(2026, 7, 14)


def _run(cfg, terms=None, filters=None, records=None, limit=20):
    return search(
        cfg, terms, filters or Filters(), limit=limit, records=records, _today=TODAY
    )


def test_recency_decay_orders_equal_matches(tmp_path):
    cfg = make_config(tmp_path)
    recent = make_record("claude/rrrr", date="2026-07-13", summary={"asked": "alpha topic"})
    old = make_record("claude/oooo", date="2026-01-01", summary={"asked": "alpha topic"})
    recs = {recent.sid: recent, old.sid: old}
    seed_store(cfg, recs.values())

    hits = _run(cfg, ["alpha"], records=recs)
    assert [h.sid for h in hits] == ["claude/rrrr", "claude/oooo"]


def test_no_terms_is_recency_ordered(tmp_path):
    cfg = make_config(tmp_path)
    a = make_record("claude/aaaa", date="2026-07-10")
    b = make_record("claude/bbbb", date="2026-05-10")
    c = make_record("claude/cccc", date="2026-07-14")
    recs = {r.sid: r for r in (a, b, c)}
    seed_store(cfg, recs.values())

    hits = _run(cfg, [], records=recs)
    assert [h.sid for h in hits] == ["claude/cccc", "claude/aaaa", "claude/bbbb"]


def test_terms_are_anded(tmp_path):
    cfg = make_config(tmp_path)
    both = make_record("claude/both", summary={"asked": "router and scheduler"})
    one = make_record("claude/one", summary={"asked": "router only"})
    recs = {both.sid: both, one.sid: one}
    seed_store(cfg, recs.values())

    hits = _run(cfg, ["router", "scheduler"], records=recs)
    assert [h.sid for h in hits] == ["claude/both"]


def test_pending_summary_still_matches_on_deterministic_fields(tmp_path):
    cfg = make_config(tmp_path)
    # Empty summary, but the term appears in keywords/cwd — must still match.
    rec = make_record(
        "claude/pppp", cwd="/home/ed/granola-extractor", keywords=["granola"]
    )
    recs = {rec.sid: rec}
    seed_store(cfg, recs.values())

    hits = _run(cfg, ["granola"], records=recs)
    assert [h.sid for h in hits] == ["claude/pppp"]


def test_zero_hits(tmp_path):
    cfg = make_config(tmp_path)
    rec = make_record("claude/aaaa", summary={"asked": "alpha"})
    recs = {rec.sid: rec}
    seed_store(cfg, recs.values())
    assert _run(cfg, ["nonexistentxyz"], records=recs) == []


def test_filters(tmp_path):
    cfg = make_config(tmp_path)
    recs = {
        "claude/aaaa": make_record(
            "claude/aaaa",
            date="2026-07-10",
            repo="1kx/dash",
            cwd="/home/ed/dash",
            model="opus-4.8",
            resolved="done",
            keywords=["nav"],
        ),
        "codex/bbbb": make_record(
            "codex/bbbb",
            date="2026-06-01",
            repo="1kx/api",
            cwd="/home/ed/api",
            model="gpt-5.6",
            resolved="open",
            keywords=["auth"],
        ),
    }
    seed_store(cfg, recs.values())

    def sids(f):
        return {h.sid for h in _run(cfg, [], filters=f, records=recs, limit=0)}

    assert sids(Filters(harness="codex")) == {"codex/bbbb"}
    assert sids(Filters(repo="dash")) == {"claude/aaaa"}
    assert sids(Filters(cwd="api")) == {"codex/bbbb"}
    assert sids(Filters(model="opus")) == {"claude/aaaa"}
    assert sids(Filters(resolved="done")) == {"claude/aaaa"}
    assert sids(Filters(keyword=["auth"])) == {"codex/bbbb"}
    assert sids(Filters(since="2026-07-01")) == {"claude/aaaa"}
    assert sids(Filters(until="2026-06-30")) == {"codex/bbbb"}


def test_filters_compose_with_terms(tmp_path):
    cfg = make_config(tmp_path)
    recs = {
        "claude/aaaa": make_record("claude/aaaa", harness="claude", summary={"asked": "shared"}),
        "codex/bbbb": make_record("codex/bbbb", harness="codex", summary={"asked": "shared"}),
    }
    seed_store(cfg, recs.values())
    hits = _run(cfg, ["shared"], filters=Filters(harness="codex"), records=recs)
    assert [h.sid for h in hits] == ["codex/bbbb"]


def test_limit(tmp_path):
    cfg = make_config(tmp_path)
    recs = {
        f"claude/{i:04d}": make_record(f"claude/{i:04d}", date=f"2026-07-{i+1:02d}")
        for i in range(5)
    }
    seed_store(cfg, recs.values())
    assert len(_run(cfg, [], records=recs, limit=3)) == 3
    assert len(_run(cfg, [], records=recs, limit=0)) == 5


def test_linear_fallback_matches_fts(tmp_path, monkeypatch):
    # Force the no-FTS5 path and confirm the same records come back.
    monkeypatch.setattr(index_mod, "_probe_features", lambda conn: (False, False))
    cfg = make_config(tmp_path)
    recs = {
        "claude/aaaa": make_record("claude/aaaa", summary={"asked": "granola sync"}),
        "codex/bbbb": make_record("codex/bbbb", summary={"asked": "nav flicker"}),
    }
    seed_store(cfg, recs.values())

    hits = _run(cfg, ["granola"], records=recs)
    assert [h.sid for h in hits] == ["claude/aaaa"]
    assert _run(cfg, ["nothinghere"], records=recs) == []
