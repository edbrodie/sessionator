"""Rendering surfaces: compact, full, ndjson."""

from __future__ import annotations

import json

from sessionator.render import render_compact, render_full, render_ndjson
from sessionator.schema import Record

from conftest import make_record


def test_compact_shows_pending_when_no_summary():
    rec = make_record("claude/aaaa1111", date="2026-07-10", model="opus-4.8")
    out = render_compact([rec])
    assert "(summary pending)" in out
    assert "2026-07-10" in out
    assert "Claude(opus-4.8)" in out
    assert "claude/aaaa1111"[:15] in out  # short_sid handle present


def test_compact_shows_asked_when_present():
    rec = make_record("claude/bbbb", summary={"asked": "wire the search layer"})
    out = render_compact([rec])
    assert "wire the search layer" in out
    assert "(summary pending)" not in out


def test_full_shows_actual_paths_and_shas():
    rec = make_record(
        "claude/cccc",
        commits=[["5f3cd4fd", "fix nav flicker"]],
        files=[["M", "src/app.py"], ["C", "src/new.py"]],
        summary={"asked": "do the thing"},
    )
    out = render_full([rec])
    assert "5f3cd4fd fix nav flicker" in out  # SHA + subject, not just a count
    assert "M src/app.py" in out
    assert "C src/new.py" in out
    assert "resume: cd" in out  # resume line present in full view


def test_ndjson_is_valid_and_roundtrips():
    recs = [
        make_record("claude/aaaa", summary={"asked": "a"}),
        make_record("codex/bbbb", summary={"asked": "b"}),
    ]
    out = render_ndjson(recs)
    lines = out.splitlines()
    assert len(lines) == 2
    for line in lines:
        d = json.loads(line)  # each line parses
        assert d["sid"]
        # round-trips back into a Record with the same sid
        assert Record.from_dict(d).sid == d["sid"]


def test_empty_render_is_empty_string():
    assert render_compact([]) == ""
    assert render_full([]) == ""
    assert render_ndjson([]) == ""
