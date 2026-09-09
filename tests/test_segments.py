"""Segment arithmetic: turn slicing, debounce, boundaries, rollup, sidecars."""

from __future__ import annotations

from sessionator import segments
from sessionator.schema import SUMMARY_FIELDS, empty_summary
from sessionator.store import Store

from conftest import make_config, make_record

EXCERPT = (
    "USER: first ask\n\n"
    "ASSISTANT: doing it\n\nstill doing it\n\n"
    "USER: second ask\n\n"
    "ASSISTANT: done"
)


def test_split_turns_keeps_blank_lines_inside_a_turn():
    turns = segments.split_turns(EXCERPT)
    assert len(turns) == 4
    assert turns[0] == "USER: first ask"
    assert turns[1] == "ASSISTANT: doing it\n\nstill doing it"
    assert segments.count_turns(EXCERPT) == 4
    assert segments.split_turns("") == []
    # Text with no role prefixes is one turn, never zero.
    assert segments.split_turns("bare text") == ["bare text"]


def test_slice_turns_clamps_out_of_range():
    turns = segments.split_turns(EXCERPT)
    assert segments.slice_turns(turns, 0, 2) == "USER: first ask\n\n" + turns[1]
    assert segments.slice_turns(turns, 2, 99) == turns[2] + "\n\n" + turns[3]
    assert segments.slice_turns(turns, 99, 120) == ""
    assert segments.slice_turns(turns, 3, 1) == ""  # inverted bounds


def test_queries_on_empty_and_populated_lists():
    assert segments.last_end([]) == 0
    assert segments.last_bytes([]) == 0
    assert segments.next_seq([]) == 1
    segs = [
        segments.make_segment(seq=1, event="precompact", trigger="auto", start=0, end=4, size=100),
        segments.make_segment(seq=2, event="session_end", trigger=None, start=4, end=9, size=250),
    ]
    assert segments.last_end(segs) == 9
    assert segments.last_bytes(segs) == 250
    assert segments.next_seq(segs) == 3
    assert len(segments.pending(segs)) == 2
    assert segments.find(segs, 2)["event"] == "session_end"
    assert segments.find(segs, 7) is None


def test_append_segment_is_debounced_until_resolved():
    rec = make_record("claude/a")
    first = segments.append_segment(rec, event="precompact", trigger="auto", turn_count=4, size=100)
    assert first["start"] == 0 and first["end"] == 4 and first["seq"] == 1
    assert first["state"] == "pending"

    # A pending segment blocks the next cut (one LLM call per growth is the bug).
    assert segments.append_segment(rec, event="change", turn_count=9, size=200) is None
    assert len(rec.summary_segments) == 1

    # Resolved -> the next cut starts where the last ended.
    segments.mark_done(rec, 1, {"asked": "a"})
    second = segments.append_segment(rec, event="change", turn_count=9, size=200)
    assert (second["start"], second["end"], second["seq"]) == (4, 9, 2)


def test_append_segment_needs_new_turns_unless_forced():
    rec = make_record("claude/a")
    segments.append_segment(rec, event="precompact", turn_count=4, size=100)
    segments.mark_done(rec, 1, {"asked": "a"})
    # No growth in turns -> nothing to summarize.
    assert segments.append_segment(rec, event="change", turn_count=4, size=180) is None
    # Forced (the user asked now) re-covers the whole excerpt instead of a
    # zero-length segment; force also overrides the debounce.
    forced = segments.append_segment(
        rec, event="manual", turn_count=4, size=180, force=True
    )
    assert (forced["start"], forced["end"], forced["event"]) == (0, 4, "manual")
    again = segments.append_segment(rec, event="manual", turn_count=4, size=180, force=True)
    assert again is not None  # pending segment does not block a forced cut
    # A forced cut on an empty session still has nothing to say.
    assert segments.append_segment(
        make_record("claude/empty"), event="manual", turn_count=0, size=0, force=True
    ) is None


def test_ensure_backfill_segment_only_for_unsegmented_records():
    rec = make_record("claude/a")
    seg = segments.ensure_backfill_segment(rec, turn_count=4, size=99)
    assert (seg["event"], seg["start"], seg["end"], seg["bytes"]) == ("backfill", 0, 4, 99)
    assert segments.ensure_backfill_segment(rec, turn_count=9, size=120) is None


def test_apply_boundaries_cuts_at_each_marker_and_ignores_junk():
    rec = make_record("codex/a")
    boundaries = [
        {"event": "precompact", "trigger": "auto", "turn": 3},
        {"event": "precompact", "trigger": "auto", "turn": 3},   # duplicate
        {"event": "precompact", "trigger": "auto", "turn": 7},
        {"event": "precompact", "trigger": "auto", "turn": 99},  # beyond the excerpt
        "junk",
    ]
    added = segments.apply_boundaries(rec, boundaries, turn_count=10, size=500)
    assert [(s["start"], s["end"]) for s in added] == [(0, 3), (3, 7)]
    # The tail after the last marker is a normal (debounce-free) cut.
    tail = segments.append_segment(
        rec, event="session_end", turn_count=10, size=500, force=True
    )
    assert (tail["start"], tail["end"]) == (7, 10)


def test_mark_done_and_error_are_idempotent():
    rec = make_record("claude/a")
    segments.append_segment(rec, event="precompact", turn_count=4, size=1)
    assert segments.mark_done(rec, 1, {"asked": "a", "bogus": "x"}) is True
    seg = rec.summary_segments[0]
    assert set(seg["summary"]) == set(SUMMARY_FIELDS)  # normalized to the five
    assert seg["summary"]["asked"] == "a"
    assert segments.mark_done(rec, 1, {"asked": "b"}) is False  # no longer pending
    assert segments.mark_error(rec, 1) is False
    assert segments.mark_done(rec, 42, {}) is False


def test_rollup_state():
    rec = make_record("claude/a", summary_state="pending")
    assert segments.rollup_state(rec) == "pending"  # no segments: unchanged

    segments.append_segment(rec, event="precompact", turn_count=4, size=1)
    assert segments.rollup_state(rec) == "pending"
    segments.mark_done(rec, 1, {"asked": "a"})
    assert segments.rollup_state(rec) == "done"

    segments.append_segment(rec, event="change", turn_count=8, size=2)
    assert segments.rollup_state(rec) == "partial"  # something shown, more coming
    segments.mark_error(rec, 2)
    assert segments.rollup_state(rec) == "done"  # a stale-but-real summary stands

    only_err = make_record("claude/b")
    segments.append_segment(only_err, event="backfill", turn_count=3, size=1)
    segments.mark_error(only_err, 1)
    assert segments.rollup_state(only_err) == "error"


def test_cap_text_middle_trims():
    assert segments.cap_text("short") == "short"
    capped = segments.cap_text("x" * 20000, 6000)
    assert len(capped) < 20000
    assert "[trimmed]" in capped


def test_segment_sidecar_roundtrip_and_deletion(tmp_path):
    cfg = make_config(tmp_path)
    store = Store(cfg)
    rec = make_record("claude/side1")
    rec.excerpt = EXCERPT
    store.write_excerpt(rec)

    path = store.write_segment_excerpt(rec, 1, "USER: only this slice")
    assert path and path.endswith("claude-side1.seg1.md")
    assert store.read_segment_excerpt(rec, 1).strip() == "USER: only this slice"
    assert store.read_segment_excerpt(rec, 2) == ""  # absent -> caller re-slices
    assert store.write_segment_excerpt(rec, 3, "   ") is None

    # A long slice is capped at write time, not left to blow the LLM budget.
    store.write_segment_excerpt(rec, 4, "y" * 20000)
    assert len(store.read_segment_excerpt(rec, 4)) < 20000

    # forget must leave no derived text behind.
    assert len(store.segment_sidecars(rec.sid)) == 2
    store.delete_excerpt(rec.sid)
    assert store.segment_sidecars(rec.sid) == []
    assert not store.excerpt_path_for(rec.sid).exists()


def test_summary_fields_helper_shape():
    # Guard the assumption mark_done leans on.
    assert set(empty_summary()) == set(SUMMARY_FIELDS)
