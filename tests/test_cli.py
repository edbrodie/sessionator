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
