"""CLI wiring: exit codes, implicit search, show ambiguity, resume one-liner."""

from __future__ import annotations

import json

import pytest

from sessionator import cli
from sessionator.config import load

from conftest import make_record, seed_store


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    """Isolate config+data under tmp XDG dirs and return a store-seeder."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    # No CLIs, so no backfill can spawn even if reconcile were reached.
    monkeypatch.setenv("PATH", "")
    cfg = load()

    def seed(records):
        seed_store(cfg, records)

    return seed


def test_implicit_search_hit_exit_0(cli_env, capsys):
    cli_env([make_record("claude/aaaa", summary={"asked": "granola sync work"})])
    code = cli.main(["granola", "--no-reconcile"])
    out = capsys.readouterr()
    assert code == 0
    assert "granola sync work" in out.out


def test_search_zero_hits_exit_1(cli_env, capsys):
    cli_env([make_record("claude/aaaa", summary={"asked": "alpha"})])
    code = cli.main(["nonexistentxyz", "--no-reconcile"])
    assert code == 1


def test_explicit_search_verb(cli_env, capsys):
    cli_env([make_record("claude/aaaa", summary={"asked": "beta topic"})])
    code = cli.main(["search", "beta", "--no-reconcile"])
    out = capsys.readouterr()
    assert code == 0
    assert "beta topic" in out.out


def test_ndjson_format_valid(cli_env, capsys):
    cli_env(
        [
            make_record("claude/aaaa", summary={"asked": "a"}),
            make_record("codex/bbbb", summary={"asked": "b"}),
        ]
    )
    code = cli.main(["--format", "ndjson", "--no-reconcile"])
    out = capsys.readouterr()
    assert code == 0
    lines = [ln for ln in out.out.splitlines() if ln.strip()]
    assert lines
    for line in lines:
        assert json.loads(line)["sid"]


def test_stdout_is_data_stderr_is_diagnostic(cli_env, capsys):
    cli_env([make_record("claude/aaaa", summary={"asked": "alpha"})])
    cli.main(["alpha", "--no-reconcile"])
    out = capsys.readouterr()
    assert "match" in out.err  # count line goes to stderr
    assert "match" not in out.out  # stdout is data only


def test_show_unique_prints_record_and_resume(cli_env, capsys):
    cli_env([make_record("claude/aaaa1111", cwd="/home/ed/x", native_id="aaaa1111")])
    code = cli.main(["show", "claude/aaaa1111", "--no-reconcile"])
    out = capsys.readouterr()
    assert code == 0
    assert "claude/aaaa1111" in out.out
    assert "cd /home/ed/x && claude --resume aaaa1111" in out.out


def test_show_ambiguous_exit_2(cli_env, capsys):
    cli_env(
        [
            make_record("claude/aaaa1111"),
            make_record("claude/aaaa2222"),
        ]
    )
    code = cli.main(["show", "claude/aaaa", "--no-reconcile"])
    out = capsys.readouterr()
    assert code == 2
    assert "ambiguous" in out.err
    assert "claude/aaaa1111" in out.err
    assert "claude/aaaa2222" in out.err


def test_show_not_found_exit_2(cli_env, capsys):
    cli_env([make_record("claude/aaaa")])
    code = cli.main(["show", "zzz/nope", "--no-reconcile"])
    assert code == 2


def test_resume_prints_exactly_one_line(cli_env, capsys):
    cli_env([make_record("codex/019f6195", cwd="/home/ed/api", native_id="019f6195")])
    code = cli.main(["resume", "codex/019f6195", "--no-reconcile"])
    out = capsys.readouterr()
    assert code == 0
    lines = [ln for ln in out.out.splitlines() if ln.strip()]
    assert lines == ["cd /home/ed/api && codex resume 019f6195"]


def test_resume_ambiguous_exit_2(cli_env, capsys):
    cli_env([make_record("claude/aaaa1111"), make_record("claude/aaaa2222")])
    code = cli.main(["resume", "claude/aaaa", "--no-reconcile"])
    assert code == 2


def test_version(capsys):
    assert cli.main(["--version"]) == 0
    assert "sessionator" in capsys.readouterr().out


# --- forget ----------------------------------------------------------------

def test_forget_sid_prefix_removes_and_tombstones(cli_env, capsys):
    from sessionator.store import Store

    cli_env([make_record("claude/keepme1111"), make_record("claude/forgetme2222")])
    cfg = load()
    store = Store(cfg)
    # Give the target an excerpt sidecar so we can prove it is deleted.
    rec = store.load()["claude/forgetme2222"]
    rec.excerpt = "USER: secret work"
    store.write_excerpt(rec)
    assert store.excerpt_path_for("claude/forgetme2222").exists()

    code = cli.main(["forget", "claude/forgetme2222"])
    out = capsys.readouterr()
    assert code == 0
    assert "forgot 1" in out.out

    records = store.load()
    assert "claude/forgetme2222" not in records
    assert "claude/keepme1111" in records  # untouched
    assert not store.excerpt_path_for("claude/forgetme2222").exists()
    assert "claude/forgetme2222" in store.load_tombstones()


def test_forget_ambiguous_prefix_exit_2(cli_env, capsys):
    cli_env([make_record("claude/aaaa1111"), make_record("claude/aaaa2222")])
    code = cli.main(["forget", "claude/aaaa"])
    out = capsys.readouterr()
    assert code == 2
    assert "ambiguous" in out.err
    # Nothing removed on an ambiguous match.
    from sessionator.store import Store

    assert len(Store(load()).load()) == 2


def test_forget_missing_exit_2(cli_env, capsys):
    cli_env([make_record("claude/aaaa1111")])
    assert cli.main(["forget", "zzz/nope"]) == 2


def test_forget_cwd_glob_matches_multiple(cli_env, capsys):
    from sessionator.store import Store

    cli_env(
        [
            make_record("claude/v1", cwd="/Users/ed/private-notes/a"),
            make_record("claude/v2", cwd="/Users/ed/private-notes"),
            make_record("claude/keep", cwd="/Users/ed/projects/other"),
        ]
    )
    code = cli.main(["forget", "**/private-notes/**"])
    out = capsys.readouterr()
    assert code == 0
    records = Store(load()).load()
    assert "claude/v1" not in records
    assert "claude/keep" in records
    tombstones = Store(load()).load_tombstones()
    assert "claude/v1" in tombstones


def test_forget_dry_run_changes_nothing(cli_env, capsys):
    from sessionator.store import Store

    cli_env([make_record("claude/forgetme2222")])
    code = cli.main(["forget", "claude/forgetme2222", "--dry-run"])
    out = capsys.readouterr()
    assert code == 0
    assert "dry-run" in out.out
    assert "claude/forgetme2222" in Store(load()).load()
    assert "claude/forgetme2222" not in Store(load()).load_tombstones()


# --- summarize -------------------------------------------------------------

ROLLUP_REPLY = (
    "@@S1@@\n"
    "- **Asked:** wire up the index\n"
    "- **Learned:** fts5 was already available\n"
    "- **Completed:** built the index\n"
    "- **Left off:** all green\n"
    "- **Next steps:** None\n"
    "- **Resolved:** done\n"
)


def _seed_summarizable(cli_env, *, with_cli=True, sid="claude/sum1111"):
    """One record with an excerpt sidecar, and (optionally) a configured CLI."""
    from sessionator import config as config_mod
    from sessionator.store import Store

    cli_env([make_record(sid, native_id=sid.split("/")[1])])
    cfg = load()
    store = Store(cfg)
    records = store.load()
    rec = records[sid]
    rec.excerpt = "USER: build the index\n\nASSISTANT: built it"
    rec.excerpt_path = store.write_excerpt(rec)
    store.write(records)
    if with_cli:
        cfg.sources["claude"].cli = "/nonexistent/claude"
        config_mod.save(cfg)
    return sid


def test_summarize_no_cli_exits_2(cli_env, capsys):
    sid = _seed_summarizable(cli_env, with_cli=False)
    code = cli.main(["summarize", sid, "--no-reconcile"])
    err = capsys.readouterr().err
    assert code == 2
    assert "no summarizer CLI" in err


def test_summarize_forces_a_manual_segment_and_prints_five_fields(
    cli_env, capsys, monkeypatch
):
    from sessionator import summarize
    from sessionator.store import Store

    sid = _seed_summarizable(cli_env)
    monkeypatch.setattr(summarize, "_invoke_cli", lambda *a, **k: ROLLUP_REPLY)

    code = cli.main(["summarize", sid, "--no-reconcile"])
    out = capsys.readouterr().out
    assert code == 0
    for label in ("Asked", "Learned", "Completed", "Left off", "Next steps"):
        assert f"{label}:" in out
    assert "wire up the index" in out

    rec = Store(load()).load()[sid]
    assert [(s["event"], s["trigger"], s["state"]) for s in rec.summary_segments] == [
        ("manual", "user", "done")
    ]
    assert rec.summary_state == "done"

    # Asking again re-cuts the whole excerpt rather than doing nothing.
    assert cli.main(["summarize", sid, "--no-reconcile"]) == 0
    rec = Store(load()).load()[sid]
    assert [s["seq"] for s in rec.summary_segments] == [1, 2]
    assert rec.summary_segments[1]["start"] == 0


def test_summarize_no_wait_cuts_and_defers(cli_env, capsys, monkeypatch):
    import sessionator.reconcile as reconcile_mod
    from sessionator.store import Store

    sid = _seed_summarizable(cli_env)
    kicked = []
    monkeypatch.setattr(
        reconcile_mod, "kick_detached_backfill",
        lambda cfg, only_sid=None: kicked.append(only_sid),
    )
    code = cli.main(["summarize", sid, "--no-wait", "--no-reconcile"])
    out = capsys.readouterr()
    assert code == 0
    assert kicked == [sid]
    rec = Store(load()).load()[sid]
    assert rec.summary_segments[0]["state"] == "pending"
    assert "summary still" in out.err  # nothing summarized yet, said on stderr


def test_summarize_reconciles_first_like_other_query_verbs(cli_env, monkeypatch):
    from sessionator import summarize

    sid = _seed_summarizable(cli_env)
    calls = []
    monkeypatch.setattr(cli, "_fast_reconcile", lambda cfg: calls.append(cfg.path))
    monkeypatch.setattr(summarize, "_invoke_cli", lambda *a, **k: ROLLUP_REPLY)
    assert cli.main(["summarize", sid]) == 0
    assert len(calls) == 1


def test_summarize_ambiguous_and_missing_exit_2(cli_env, capsys):
    cli_env([make_record("claude/aaaa1111"), make_record("claude/aaaa2222")])
    assert cli.main(["summarize", "claude/aaaa", "--no-reconcile"]) == 2
    assert "ambiguous" in capsys.readouterr().err
    assert cli.main(["summarize", "zzz/nope", "--no-reconcile"]) == 2


def test_backfill_accepts_sid(cli_env, capsys, monkeypatch):
    from sessionator import summarize

    seen = {}
    monkeypatch.setattr(
        summarize, "backfill",
        lambda *a, **kw: seen.update(kw) or {"selected": 0},
    )
    assert cli.main(["_backfill", "--sid", "claude/aaaa"]) == 0
    assert seen["only_sid"] == "claude/aaaa"


def test_status_reports_segments_and_model(cli_env, capsys, monkeypatch):
    from sessionator import segments as seg
    from sessionator.store import Store

    sid = _seed_summarizable(cli_env)
    store = Store(load())
    records = store.load()
    seg.append_segment(records[sid], event="precompact", turn_count=2, size=10)
    records[sid].summary_state = "partial"
    store.write(records)

    monkeypatch.setattr(cli, "_fast_reconcile", lambda cfg: None)
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "segments:   1 in 1 session(s), 1 pending" in out
    assert "1 pending, 0 errored" in out  # partial counts as pending
    assert "prefer claude" in out
    assert "claude via haiku" in out
