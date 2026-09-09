"""`sessionator setup codex|status`: merging into a file the user owns.

Every test runs against a temp ``CODEX_HOME``/``HOME`` with a fake ``sessionator``
on ``PATH``, so nothing here can reach the real ``~/.codex``.

The property under test throughout is restraint: our two handlers go in and come
out cleanly, and everything else in the file — foreign events, foreign groups,
foreign keys, and the whole of ``config.toml`` — is exactly as it was.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sessionator import cli, setup_hooks as sh


@pytest.fixture
def codex_env(tmp_path, monkeypatch):
    """Temp CODEX_HOME + XDG dirs + a fake `sessionator` on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "sessionator"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)

    home = tmp_path / "home"
    home.mkdir()
    codex = tmp_path / "codex"
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(codex))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(sh.Path, "home", staticmethod(lambda: home))
    return codex


@pytest.fixture
def hooks_file(codex_env):
    return codex_env / "hooks.json"


def _command():
    return sh.hook_command(sh.resolve_cli())


def _read(path):
    return json.loads(path.read_text())


def _our_handlers(data, command):
    return [
        h
        for groups in data.get("hooks", {}).values()
        for _gi, _hi, h in sh._iter_handlers(groups)
        if h.get("command") == command
    ]


# --- the command string -----------------------------------------------------

def test_command_is_the_absolute_cli_plus_ingest_hook(codex_env):
    command = _command()
    assert command.endswith(" ingest --hook")
    assert Path(command.split(" ingest")[0]).is_absolute()


def test_no_cli_on_path_exits_2_with_an_install_hint(codex_env, monkeypatch, capsys):
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(cli.sys, "argv", ["pytest"])
    assert cli.main(["setup", "codex"]) == 2
    err = capsys.readouterr().err
    assert "no `sessionator` executable" in err
    assert "uv tool install" in err


# --- creating the file ------------------------------------------------------

def test_creates_both_handlers_exactly(codex_env, hooks_file, capsys):
    assert cli.main(["setup", "codex"]) == 0
    data = _read(hooks_file)
    command = _command()

    assert data["description"] == sh.DESCRIPTION
    assert data["hooks"]["SessionEnd"] == [
        {"hooks": [{"type": "command", "command": command, "timeout": 3}]}
    ]
    assert data["hooks"]["PreCompact"] == [
        {
            "matcher": "*",
            "hooks": [
                {
                    "type": "command",
                    "command": command,
                    "async": True,
                    "timeout": 600,
                }
            ],
        }
    ]


def test_handlers_carry_no_custom_ownership_keys(codex_env, hooks_file):
    cli.main(["setup", "codex"])
    data = _read(hooks_file)
    command = _command()
    for handler in _our_handlers(data, command):
        # Ownership is the command string alone — Codex would not understand a
        # marker key and the user should not have to look at one.
        assert set(handler) <= {"type", "command", "async", "timeout"}


def test_prints_the_trust_instructions_and_the_positional_caveat(codex_env, capsys):
    cli.main(["setup", "codex"])
    out = capsys.readouterr().out
    assert "/hooks" in out
    assert "positional" in out.lower()


# --- idempotence and merging ------------------------------------------------

def test_second_run_changes_nothing(codex_env, hooks_file, capsys):
    cli.main(["setup", "codex"])
    first = hooks_file.read_bytes()
    capsys.readouterr()

    assert cli.main(["setup", "codex"]) == 0
    out = capsys.readouterr().out
    assert "already up to date" in out
    assert hooks_file.read_bytes() == first


def test_our_group_is_appended_after_foreign_groups(codex_env, hooks_file):
    foreign_end = {"hooks": [{"type": "command", "command": "/usr/bin/true"}]}
    foreign_pre = {"matcher": "*", "hooks": [{"type": "command", "command": "/bin/ls"}]}
    hooks_file.parent.mkdir(parents=True)
    hooks_file.write_text(json.dumps({
        "description": "the user's own file",
        "hooks": {
            "SessionEnd": [foreign_end],
            "PreCompact": [foreign_pre],
            "Stop": [{"hooks": [{"type": "command", "command": "/bin/echo"}]}],
        },
        "somethingElse": {"keep": "me"},
    }))

    assert cli.main(["setup", "codex"]) == 0
    data = _read(hooks_file)

    # Foreign groups keep their positions, so their trust hashes survive.
    assert data["hooks"]["SessionEnd"][0] == foreign_end
    assert data["hooks"]["PreCompact"][0] == foreign_pre
    assert len(data["hooks"]["SessionEnd"]) == 2
    assert len(data["hooks"]["PreCompact"]) == 2
    # Foreign events and foreign top-level keys are untouched.
    assert data["hooks"]["Stop"] == [
        {"hooks": [{"type": "command", "command": "/bin/echo"}]}
    ]
    assert data["somethingElse"] == {"keep": "me"}
    # A description we did not write is not replaced.
    assert data["description"] == "the user's own file"


def test_stale_handler_is_rewritten_in_place(codex_env, hooks_file):
    command = _command()
    hooks_file.parent.mkdir(parents=True)
    hooks_file.write_text(json.dumps({
        "hooks": {
            "SessionEnd": [
                {"hooks": [{"type": "command", "command": "/bin/echo"}]},
                # Ours, but with a stale timeout.
                {"hooks": [{"type": "command", "command": command, "timeout": 99}]},
                {"hooks": [{"type": "command", "command": "/bin/date"}]},
            ]
        }
    }))

    assert cli.main(["setup", "codex"]) == 0
    groups = _read(hooks_file)["hooks"]["SessionEnd"]
    # Same slot, corrected content: no group moved, so no re-trust is needed.
    assert len(groups) == 3
    assert groups[1]["hooks"][0]["timeout"] == 3
    assert groups[0]["hooks"][0]["command"] == "/bin/echo"
    assert groups[2]["hooks"][0]["command"] == "/bin/date"


def test_handler_sharing_a_group_with_a_foreign_hook_is_rewritten_not_moved(
    codex_env, hooks_file
):
    command = _command()
    hooks_file.parent.mkdir(parents=True)
    hooks_file.write_text(json.dumps({
        "hooks": {
            "PreCompact": [
                {
                    "matcher": "*",
                    "hooks": [
                        {"type": "command", "command": "/bin/echo"},
                        {"type": "command", "command": command},
                    ],
                }
            ]
        }
    }))

    cli.main(["setup", "codex"])
    groups = _read(hooks_file)["hooks"]["PreCompact"]
    assert len(groups) == 1
    assert groups[0]["hooks"][0]["command"] == "/bin/echo"
    assert groups[0]["hooks"][1]["async"] is True


def test_duplicate_handlers_collapse_to_one(codex_env, hooks_file):
    command = _command()
    dup = {"hooks": [{"type": "command", "command": command}]}
    hooks_file.parent.mkdir(parents=True)
    hooks_file.write_text(json.dumps({"hooks": {"SessionEnd": [dup, dup]}}))

    cli.main(["setup", "codex"])
    data = _read(hooks_file)
    assert len(_our_handlers(data, command)) == 2  # one SessionEnd + one PreCompact


# --- dry run ----------------------------------------------------------------

def test_dry_run_writes_nothing(codex_env, hooks_file, capsys):
    assert cli.main(["setup", "codex", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert not hooks_file.exists()
    assert not hooks_file.parent.exists()
    assert "dry-run" in out
    assert "SessionEnd: added" in out
    # The resulting file is shown in full, so the user can read it before it lands.
    assert json.loads(out[out.index("{"):])["hooks"]["PreCompact"]


def test_dry_run_on_an_existing_file_leaves_it_byte_identical(codex_env, hooks_file):
    hooks_file.parent.mkdir(parents=True)
    original = json.dumps({"hooks": {"Stop": [{"hooks": []}]}})
    hooks_file.write_text(original)
    assert cli.main(["setup", "codex", "--dry-run"]) == 0
    assert hooks_file.read_text() == original


# --- removal ----------------------------------------------------------------

def test_remove_deletes_a_file_that_was_only_ours(codex_env, hooks_file, capsys):
    cli.main(["setup", "codex"])
    assert hooks_file.exists()
    capsys.readouterr()

    assert cli.main(["setup", "codex", "--remove"]) == 0
    assert not hooks_file.exists()
    assert "config.toml were not touched" in capsys.readouterr().out


def test_remove_keeps_everything_foreign(codex_env, hooks_file):
    foreign = {"hooks": [{"type": "command", "command": "/usr/bin/true"}]}
    hooks_file.parent.mkdir(parents=True)
    hooks_file.write_text(json.dumps({
        "description": "the user's own file",
        "hooks": {"SessionEnd": [foreign], "Stop": [foreign]},
    }))
    cli.main(["setup", "codex"])

    assert cli.main(["setup", "codex", "--remove"]) == 0
    data = _read(hooks_file)
    assert data["hooks"]["SessionEnd"] == [foreign]
    assert data["hooks"]["Stop"] == [foreign]
    # The event that held only our handler is gone, not left as an empty list.
    assert "PreCompact" not in data["hooks"]
    assert data["description"] == "the user's own file"


def test_remove_leaves_a_shared_group_intact(codex_env, hooks_file):
    command = _command()
    hooks_file.parent.mkdir(parents=True)
    hooks_file.write_text(json.dumps({
        "hooks": {
            "PreCompact": [
                {
                    "matcher": "*",
                    "hooks": [
                        {"type": "command", "command": "/bin/echo"},
                        {"type": "command", "command": command},
                    ],
                }
            ]
        }
    }))
    cli.main(["setup", "codex", "--remove"])
    groups = _read(hooks_file)["hooks"]["PreCompact"]
    assert groups == [{"matcher": "*", "hooks": [
        {"type": "command", "command": "/bin/echo"}
    ]}]


def test_remove_is_a_no_op_when_nothing_is_ours(codex_env, hooks_file, capsys):
    hooks_file.parent.mkdir(parents=True)
    original = json.dumps({"hooks": {"Stop": [{"hooks": []}]}})
    hooks_file.write_text(original)
    assert cli.main(["setup", "codex", "--remove"]) == 0
    assert "nothing to remove" in capsys.readouterr().out
    assert hooks_file.read_text() == original


def test_remove_without_a_file_is_a_no_op(codex_env, hooks_file, capsys):
    assert cli.main(["setup", "codex", "--remove"]) == 0
    assert "does not exist" in capsys.readouterr().out


def test_remove_dry_run_writes_nothing(codex_env, hooks_file, capsys):
    cli.main(["setup", "codex"])
    before = hooks_file.read_bytes()
    capsys.readouterr()
    assert cli.main(["setup", "codex", "--remove", "--dry-run"]) == 0
    assert hooks_file.read_bytes() == before
    assert "dry-run" in capsys.readouterr().out


# --- refusing what we do not understand -------------------------------------

@pytest.mark.parametrize("body", ['{"hooks": ', '["not", "an", "object"]', "nope"])
def test_corrupt_file_exits_2_and_is_left_untouched(codex_env, hooks_file, capsys, body):
    hooks_file.parent.mkdir(parents=True)
    hooks_file.write_text(body)
    assert cli.main(["setup", "codex"]) == 2
    assert hooks_file.read_text() == body
    assert "Refusing to overwrite" in capsys.readouterr().err


def test_empty_file_is_treated_as_a_fresh_start(codex_env, hooks_file):
    hooks_file.parent.mkdir(parents=True)
    hooks_file.write_text("   \n")
    assert cli.main(["setup", "codex"]) == 0
    assert _read(hooks_file)["hooks"]["SessionEnd"]


# --- config.toml is never touched -------------------------------------------

CONFIG_TOML = """\
model = "gpt-5.6-luna"

[[hooks.SessionEnd]]
command = "/usr/local/bin/my-own-hook"

[[hooks.Stop]]
command = "/usr/local/bin/another"
"""


def test_config_toml_is_never_written(codex_env, hooks_file):
    codex_env.mkdir(parents=True, exist_ok=True)
    toml = codex_env / "config.toml"
    toml.write_text(CONFIG_TOML)

    cli.main(["setup", "codex"])
    cli.main(["setup", "status"])
    cli.main(["setup", "codex", "--remove"])
    assert toml.read_text() == CONFIG_TOML


def test_config_toml_hook_tables_are_counted(codex_env):
    codex_env.mkdir(parents=True, exist_ok=True)
    (codex_env / "config.toml").write_text(CONFIG_TOML)
    assert sh.count_config_toml_hooks() == 2
    assert sh.count_config_toml_hooks(codex_env / "absent.toml") == 0


# --- status -----------------------------------------------------------------

def test_status_reports_every_leg_of_capture(codex_env, capsys):
    codex_env.mkdir(parents=True, exist_ok=True)
    (codex_env / "config.toml").write_text(CONFIG_TOML)
    cli.main(["setup", "codex"])
    capsys.readouterr()

    assert cli.main(["setup", "status"]) == 0
    out = capsys.readouterr().out
    assert "PreCompact" in out and "SessionEnd" in out
    assert "config.toml: 2 hook table(s)" in out
    assert "spool:      0 pending" in out
    assert "summarizer: prefer" in out
    assert "haiku" in out


def test_status_before_anything_is_installed(codex_env, capsys):
    assert cli.main(["setup", "status"]) == 0
    out = capsys.readouterr().out
    assert "absent" in out
    assert "no sessionator hook found" in out


def test_status_finds_a_claude_plugin_hook(codex_env, tmp_path, capsys):
    plugin_hooks = (
        Path(tmp_path) / "home" / ".claude" / "plugins" / "market" / "sessionator"
        / "hooks"
    )
    plugin_hooks.mkdir(parents=True)
    (plugin_hooks / "hooks.json").write_text(
        json.dumps({"hooks": {"SessionEnd": [{"hooks": [
            {"type": "command", "command": "bash ingest-hook.sh sessionator"}
        ]}]}})
    )
    assert cli.main(["setup", "status"]) == 0
    assert "hook found in" in capsys.readouterr().out


def test_status_survives_a_corrupt_codex_hooks_file(codex_env, hooks_file, capsys):
    hooks_file.parent.mkdir(parents=True)
    hooks_file.write_text("{{{")
    assert cli.main(["setup", "status"]) == 0
    assert "unreadable" in capsys.readouterr().out


def test_setup_rejects_an_unknown_target(codex_env, capsys):
    assert cli.main(["setup", "wat"]) == 2


def test_quoted_hook_executes_and_migrates_without_duplicates(tmp_path):
    import subprocess
    executable = tmp_path / "folder with spaces" / "sessionator"
    executable.parent.mkdir()
    executable.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    executable.chmod(0o755)
    command = sh.hook_command(str(executable))
    result = subprocess.run(command, shell=True, capture_output=True, text=True)
    assert result.returncode == 0
    assert result.stdout.splitlines() == ['ingest', '--hook']
    legacy = f'{executable} ingest --hook'
    old = sh.merge(None, legacy).data
    updated = sh.merge(old, command).data
    assert len(sh.find_ours(updated, command)['SessionEnd']) == 1
    assert sh.merge(updated, command).changed is False
    assert sh.is_empty(sh.unmerge(updated, command).data)
    assert sh.is_empty(sh.unmerge(old, command).data)
