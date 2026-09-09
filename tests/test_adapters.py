import json
from pathlib import Path

import pytest

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


def _write_rollout(tmp_path, lines):
    d = tmp_path / "codex" / "2026" / "07" / "11"
    d.mkdir(parents=True)
    uuid = "019f7777-0000-7000-8000-000000000009"
    meta = {
        "type": "session_meta",
        "payload": {
            "id": uuid,
            "session_id": "shared-thread-009",
            "originator": "codex-tui",
            "thread_source": "user",
            "cwd": "/home/u/proj",
        },
    }
    p = d / f"rollout-2026-07-11T09-00-00-{uuid}.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in [meta, *lines]) + "\n")
    return p


def _user(msg, ts="2026-07-11T09:00:01Z"):
    return {
        "type": "event_msg",
        "timestamp": ts,
        "payload": {"type": "user_message", "message": msg},
    }


def _agent(msg):
    return {"type": "event_msg", "payload": {"type": "agent_message", "message": msg}}


def test_codex_compacted_line_marks_a_boundary(tmp_path):
    path = _write_rollout(
        tmp_path,
        [
            _user("start the parser work"),
            _agent("on it"),
            {"type": "compacted"},
            {"type": "compacted"},  # collapses: same turn ordinal
            _user("now finish it"),
            _agent("done"),
        ],
    )
    cfg = make_config(tmp_path, codex_dir=tmp_path / "codex")
    rec = codex.extract(path, cfg)
    assert rec is not None
    # The cut sits after the two turns that preceded the compaction.
    assert rec.boundaries == [{"event": "precompact", "trigger": "auto", "turn": 2}]
    assert rec.turn_count == 4
    assert rec.excerpt_full and rec.excerpt_full == rec.excerpt


def test_walker_boundary_before_any_turn_is_dropped():
    from sessionator.adapters._common import Walker

    w = Walker()
    w.mark_boundary("precompact", "auto")  # nothing to cut yet
    assert w.boundaries == []
    w.add_user("hello")
    w.mark_boundary("precompact", "auto")
    assert w.boundaries == [{"event": "precompact", "trigger": "auto", "turn": 1}]


# --- codex client filtering: a denylist, not an allowlist -------------------

DESKTOP_UUID = "019fd35c-0000-7000-8000-00000000000d"


def _desktop_session(root):
    return (
        root / "2026" / "07" / "12"
        / f"rollout-2026-07-12T14-00-00-{DESKTOP_UUID}.jsonl"
    )


def test_codex_desktop_session_is_ingested(tmp_path, codex_desktop_fixtures):
    # The regression this denylist exists for: `Codex Desktop` is ~99% of this
    # user's Codex sessions and the old `originator == "codex-tui"` allowlist
    # dropped every one of them.
    cfg = make_config(tmp_path, codex_dir=codex_desktop_fixtures)
    rec = codex.extract(_desktop_session(codex_desktop_fixtures), cfg)
    assert rec is not None
    assert rec.sid == f"codex/{DESKTOP_UUID}"
    assert rec.client == "Codex Desktop"
    assert rec.model == "gpt-5.6-luna"
    assert any(sha == "7ac9911" for sha, _ in rec.commits)


def test_claude_records_carry_their_client(tmp_path, claude_fixtures):
    cfg = make_config(tmp_path, claude_dir=claude_fixtures)
    rec = claude.extract(_claude_session(claude_fixtures), cfg)
    assert rec.client == "claude-code"


@pytest.mark.parametrize(
    "originator,denied",
    [
        ("codex-exec", True),
        ("codex_exec", True),        # normalized: underscores are hyphens
        ("Codex Exec", True),        # normalized: spaces and case
        ("codex-subagent", True),
        ("codex-mcp", True),
        ("codex-cloud", True),
        ("codex-automation", True),
        ("", True),                  # every real client sets one
        ("   ", True),
        (None, True),
        (7, True),
        ("codex-tui", False),
        ("Codex Desktop", False),
        ("codex-vscode-2029", False),  # a client that does not exist yet
    ],
)
def test_originator_denylist(originator, denied):
    assert codex._originator_denied(originator) is denied


def test_unknown_originator_with_user_turns_is_ingested(tmp_path):
    path = _write_rollout(tmp_path, [_user("hello from the future"), _agent("hi")])
    text = path.read_text().replace('"codex-tui"', '"codex-neuralink"')
    path.write_text(text)
    cfg = make_config(tmp_path, codex_dir=tmp_path / "codex")
    rec = codex.extract(path, cfg)
    assert rec is not None and rec.client == "codex-neuralink"


def test_subagent_thread_source_still_filtered_whatever_the_client(tmp_path):
    path = _write_rollout(tmp_path, [_user("nested work"), _agent("ok")])
    path.write_text(path.read_text().replace('"user"', '"subagent"'))
    cfg = make_config(tmp_path, codex_dir=tmp_path / "codex")
    assert codex.extract(path, cfg) is None


# --- archived_sessions + watermark identity --------------------------------

def test_discover_sources_includes_the_archive_sibling(tmp_path):
    home = tmp_path / "codex-home"
    (home / "sessions" / "2026" / "07" / "12").mkdir(parents=True)
    archive = home / "archived_sessions"
    archive.mkdir()
    cfg = make_config(tmp_path, codex_dir=home / "sessions")

    assert codex.discover_sources(cfg) == [home / "sessions", archive]

    # Absent archive dir: just the one root, no error.
    archive.rmdir()
    assert codex.discover_sources(cfg) == [home / "sessions"]


def test_archived_rollouts_are_enumerated(tmp_path, codex_desktop_fixtures):
    import shutil

    home = tmp_path / "codex-home"
    (home / "sessions").mkdir(parents=True)
    archive = home / "archived_sessions"
    archive.mkdir()
    # Archiving flattens: the dated dirs are not preserved.
    shutil.copy(_desktop_session(codex_desktop_fixtures), archive)

    cfg = make_config(tmp_path, codex_dir=home / "sessions")
    found = [
        p
        for root in codex.discover_sources(cfg)
        for p, _m, _s in codex.enumerate_sessions(root)
    ]
    assert [p.parent for p in found] == [archive]


def test_watermark_key_is_stable_across_an_archive_move(tmp_path):
    from sessionator.store import watermark_key

    name = f"rollout-2026-07-12T14-00-00-{DESKTOP_UUID}.jsonl"
    live = tmp_path / "sessions" / "2026" / "07" / "12" / name
    archived = tmp_path / "archived_sessions" / name

    assert codex.watermark_key(live) == f"codex:{DESKTOP_UUID}"
    assert codex.watermark_key(live) == codex.watermark_key(archived)
    assert watermark_key(codex, live) == f"codex:{DESKTOP_UUID}"


def test_claude_watermark_key_is_the_session_uuid(tmp_path, claude_fixtures):
    from sessionator.store import watermark_key

    path = _claude_session(claude_fixtures)
    assert claude.watermark_key(path) == "claude:4f00dc4e-0d36-4bd9-a481-39ef82c19509"
    assert watermark_key(claude, path) == claude.watermark_key(path)


def test_watermark_key_falls_back_for_an_adapter_without_one():
    from sessionator.store import watermark_key

    class Bare:
        pass

    class Broken:
        @staticmethod
        def watermark_key(path):
            raise RuntimeError("nope")

    assert watermark_key(Bare, "/x/y.jsonl") == "path:/x/y.jsonl"
    assert watermark_key(Broken, "/x/y.jsonl") == "path:/x/y.jsonl"
    assert watermark_key(None, "/x/y.jsonl") == "path:/x/y.jsonl"
