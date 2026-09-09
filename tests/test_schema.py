import pytest

from sessionator import schema
from sessionator.schema import Record, empty_summary


def test_make_and_split_sid():
    sid = schema.make_sid("claude", "b4ae266f-1234")
    assert sid == "claude/b4ae266f-1234"
    assert schema.split_sid(sid) == ("claude", "b4ae266f-1234")
    assert schema.sid_uuid(sid) == "b4ae266f-1234"


def test_short_sid_truncates_uuid_only():
    sid = schema.make_sid("codex", "019f5d38-8c93-7f42")
    assert schema.short_sid(sid) == "codex/019f5d38"


@pytest.mark.parametrize("bad", ["", "claude", "/uuid", "claude/", "codex"])
def test_split_sid_rejects_malformed(bad):
    with pytest.raises(ValueError):
        schema.split_sid(bad)


def test_roundtrip_excludes_excerpt():
    rec = Record(sid="claude/x", harness="claude", native_id="x", date="2026-07-10")
    rec.excerpt = "USER: hi"
    d = rec.to_dict()
    assert "excerpt" not in d
    assert d["schema_version"] == schema.SCHEMA_VERSION
    assert list(d)[0] == "schema_version"  # canonical field order
    back = Record.from_dict(d)
    assert back.sid == "claude/x"
    assert back.summary == empty_summary()


def test_validate_ok():
    rec = Record(
        sid="claude/x", harness="claude", native_id="x", date="2026-07-10",
    )
    assert schema.validate(rec) == []


def test_validate_flags_problems():
    rec = Record(sid="weird", harness="martian", native_id="x", date="")
    problems = schema.validate(rec)
    assert any("sid" in p for p in problems)
    assert any("harness" in p for p in problems)
    assert any("date" in p for p in problems)


def test_from_dict_tolerates_missing_optionals():
    rec = Record.from_dict({"sid": "codex/y", "harness": "codex"})
    assert rec.native_id == ""
    assert rec.resolved == "unknown"
    assert rec.summary == empty_summary()
    assert rec.summary_state == "pending"


# --- segments / client / partial --------------------------------------------


def _seg(**over):
    seg = {
        "seq": 1,
        "event": "precompact",
        "trigger": "auto",
        "start": 0,
        "end": 12,
        "bytes": 4096,
        "at": "2026-09-09T10:00:00+02:00",
        "state": "pending",
        "summary": None,
    }
    seg.update(over)
    return seg


def _rec(**over):
    kw = dict(sid="claude/x", harness="claude", native_id="x", date="2026-07-10")
    kw.update(over)
    return Record(**kw)


def test_new_fields_default_and_persist():
    rec = _rec()
    assert rec.summary_segments == []
    assert rec.client is None
    assert rec.boundaries == []
    d = rec.to_dict()
    assert d["summary_segments"] == []
    assert d["client"] is None
    assert "boundaries" not in d  # transient, like excerpt


def test_segments_roundtrip_and_legacy_dicts_load():
    rec = _rec(summary_segments=[_seg()], client="Codex Desktop")
    back = Record.from_dict(rec.to_dict())
    assert back.summary_segments == [_seg()]
    assert back.client == "Codex Desktop"
    # A record written before segments existed still loads.
    legacy = Record.from_dict({"sid": "claude/y", "harness": "claude"})
    assert legacy.summary_segments == []
    assert legacy.client is None


def test_validate_accepts_good_segments_and_partial_state():
    rec = _rec(
        summary_segments=[_seg(), _seg(seq=2, event="session_end", start=12, end=30)],
        summary_state="partial",
        client="claude-code",
    )
    assert schema.validate(rec) == []


def test_validate_rejects_bad_segments():
    assert any("event" in p for p in schema.validate(_rec(summary_segments=[_seg(event="nope")])))
    assert any("state" in p for p in schema.validate(_rec(summary_segments=[_seg(state="wat")])))
    assert any("seq" in p for p in schema.validate(_rec(summary_segments=[_seg(seq="1")])))
    assert any("start" in p for p in schema.validate(_rec(summary_segments=[_seg(start=9, end=2)])))
    assert any("not an object" in p for p in schema.validate(_rec(summary_segments=["x"])))
    assert any("five fields" in p for p in schema.validate(
        _rec(summary_segments=[_seg(summary={"asked": "a"})])
    ))
    assert any("list" in p for p in schema.validate(_rec(summary_segments={})))
    assert any("client" in p for p in schema.validate(_rec(client=7)))
