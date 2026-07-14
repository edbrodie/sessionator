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
