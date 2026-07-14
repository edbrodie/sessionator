from pathlib import Path

from sessionator.adapters import ADAPTERS, claude, codex
from conftest import make_config


def _claude_session(claude_fixtures):
    return claude_fixtures / "-home-u-proj" / "4f00dc4e-0d36-4bd9-a481-39ef82c19509.jsonl"


def test_registry_has_both():
    assert set(ADAPTERS) == {"claude", "codex"}


def test_claude_extract(tmp_path, claude_fixtures):
    cfg = make_config(tmp_path, claude_dir=claude_fixtures)
    rec = claude.extract(_claude_session(claude_fixtures), cfg)
    assert rec is not None
    assert rec.sid == "claude/4f00dc4e-0d36-4bd9-a481-39ef82c19509"
    assert rec.harness == "claude"
    assert rec.native_id == "4f00dc4e-0d36-4bd9-a481-39ef82c19509"
    assert rec.cwd == "/home/u/proj"
    assert rec.date == "2026-07-10"
    assert rec.model == "claude-fable-5"
    assert ["M", "auth.py"] in rec.files
    assert any(sha == "abc1234" for sha, _ in rec.commits)
    assert "https://github.com/acme/proj/pull/42" in rec.prs
    assert rec.tests == {"text": "3 passed", "broken": False}
    assert rec.resolved == "done"
    # Private span stripped from the excerpt.
    assert "sk-secret-abc123" not in rec.excerpt
    assert "[private]" in rec.excerpt


def test_claude_sidechain_filtered(tmp_path, claude_fixtures):
    cfg = make_config(tmp_path, claude_dir=claude_fixtures)
    sidechain = claude_fixtures / "-home-u-proj" / "aaaa1111-bbbb-2222-cccc-333344445555.jsonl"
    assert claude.extract(sidechain, cfg) is None


def test_claude_enumerate_skips_subagents(tmp_path, claude_fixtures):
    cfg = make_config(tmp_path, claude_dir=claude_fixtures)
    roots = claude.discover_sources(cfg)
    assert roots
    files = [p for p, _m, _s in claude.enumerate_sessions(roots[0])]
    assert all("subagents" not in Path(p).parts for p in files)


def test_codex_extract(tmp_path, codex_fixtures):
    cfg = make_config(tmp_path, codex_dir=codex_fixtures)
    session = (
        codex_fixtures / "2026" / "07" / "10"
        / "rollout-2026-07-10T09-00-00-019f5d38-8c93-7f42-8c83-2cc98d365537.jsonl"
    )
    rec = codex.extract(session, cfg)
    assert rec is not None
    # sid uuid is the rollout top-level id, NOT the shared session_id.
    assert rec.sid == "codex/019f5d38-8c93-7f42-8c83-2cc98d365537"
    assert rec.native_id == "019f5d38-8c93-7f42-8c83-2cc98d365537"
    assert rec.forked_from is None
    assert rec.model == "gpt-5.6-luna"
    assert any(sha == "def5678" for sha, _ in rec.commits)
    assert "https://github.com/acme/proj/pull/43" in rec.prs
    assert rec.resolved == "done"
    assert "tok-xyz-999" not in rec.excerpt
    assert "[private]" in rec.excerpt


def test_codex_headless_filtered(tmp_path, codex_headless_fixtures):
    cfg = make_config(tmp_path, codex_dir=codex_headless_fixtures)
    session = (
        codex_headless_fixtures / "2026" / "07" / "10"
        / "rollout-2026-07-10T11-00-00-019f6000-0000-7000-8000-000000000001.jsonl"
    )
    assert codex.extract(session, cfg) is None


def test_malformed_line_counts_warning(tmp_path, claude_fixtures):
    # A file with one bad line and one good user turn.
    d = tmp_path / "proj"
    d.mkdir()
    f = d / "dddd4444-eeee-5555-ffff-666677778888.jsonl"
    f.write_text(
        "{not valid json}\n"
        '{"type":"user","timestamp":"2026-07-10T09:00:00Z","cwd":"/home/u/x",'
        '"message":{"role":"user","content":"just a question"}}\n'
    )
    cfg = make_config(tmp_path, claude_dir=tmp_path)
    rec = claude.extract(f, cfg)
    assert rec is not None
    assert rec.parse_warnings == 1
