"""Resume-string exactness for both harnesses."""

from __future__ import annotations

from sessionator.resume import resume_string

from conftest import make_record


def test_claude_resume_string_exact():
    rec = make_record(
        "claude/d78b1004-aba0", cwd="/Users/ed/proj", native_id="d78b1004-aba0"
    )
    assert resume_string(rec) == "cd /Users/ed/proj && claude --resume d78b1004-aba0"


def test_codex_resume_string_exact():
    rec = make_record(
        "codex/019f6195-7837", cwd="/Users/ed/api", native_id="019f6195-7837"
    )
    assert resume_string(rec) == "cd /Users/ed/api && codex resume 019f6195-7837"


def test_resume_string_quotes_cwd_with_spaces():
    rec = make_record(
        "claude/abcd", cwd="/Users/ed/My Project", native_id="abcd"
    )
    out = resume_string(rec)
    assert out == "cd '/Users/ed/My Project' && claude --resume abcd"


def test_resume_string_is_single_line():
    rec = make_record("codex/xyz", cwd="/tmp/x", native_id="xyz")
    assert "\n" not in resume_string(rec)
