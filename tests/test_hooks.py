"""Hook ingest: the spool/detach front half, and the worker back half.

The front half runs inside a harness hook on a sub-second budget and its stdout
is part of the harness's protocol, so its contract is narrow and absolute:
**silent, exit 0, fast** — even when everything under it fails.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from sessionator import cli, hooks
from sessionator.adapters import adapter_for_path, claude, codex
from sessionator.store import Store

from conftest import make_config


# --- payload parsing --------------------------------------------------------

def test_parse_payload_accepts_bytes_and_str():
    raw = b'{"hook_event_name":"PreCompact","trigger":"auto"}'
    assert hooks.parse_payload(raw)["trigger"] == "auto"
    assert hooks.parse_payload(raw.decode())["hook_event_name"] == "PreCompact"


@pytest.mark.parametrize("raw", [b"", b"   ", b"not json", b'["a"]', b"null", None])
def test_parse_payload_never_raises_on_junk(raw):
    assert hooks.parse_payload(raw) == {}


@pytest.mark.parametrize(
    "name,event",
    [
        ("PreCompact", "precompact"),
        ("PostCompact", "postcompact"),
        ("SessionEnd", "session_end"),
        ("Stop", "stop"),
        ("SessionStart", "session_start"),
        ("Notification", "change"),   # unknown event is still a cut point
        (None, "change"),
    ],
)
def test_event_mapping(name, event):
    assert hooks.event_for({"hook_event_name": name}) == event


def test_trigger_precedence_and_absence():
    assert hooks.trigger_for({"trigger": "auto", "reason": "exit"}) == "auto"
    assert hooks.trigger_for({"reason": "clear"}) == "clear"
    assert hooks.trigger_for({"source": "compact"}) == "compact"
    assert hooks.trigger_for({"trigger": "  "}) is None
    assert hooks.trigger_for({}) is None


# --- adapter_for_path -------------------------------------------------------

def test_adapter_for_path(tmp_path):
    cfg = make_config(tmp_path, claude_dir=tmp_path / "projects")
    rollout = tmp_path / "anywhere" / "rollout-2026-07-10T09-00-00-abc.jsonl"
    dashed = tmp_path / "elsewhere" / "-home-u-proj" / "u.jsonl"
    in_projects = tmp_path / "projects" / "nested" / "u.jsonl"

    assert adapter_for_path(cfg, rollout) is codex
    assert adapter_for_path(cfg, dashed) is claude
    assert adapter_for_path(cfg, in_projects) is claude
    # Unrecognized shapes are never ingested by guesswork.
    assert adapter_for_path(cfg, tmp_path / "notes.jsonl") is None
    assert adapter_for_path(cfg, tmp_path / "projects" / "x.txt") is None


# --- spool + detach ---------------------------------------------------------

@pytest.fixture
def hook_env(tmp_path, monkeypatch):
    """Isolate config+data under tmp XDG dirs; return the loaded config."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("PATH", "")
    from sessionator.config import load

    return load()


class _FakePopen:
    calls: list = []

    def __init__(self, args, **kw):
        type(self).calls.append((list(args), kw))


@pytest.fixture
def fake_popen(monkeypatch):
    _FakePopen.calls = []
    monkeypatch.setattr(hooks.subprocess, "Popen", _FakePopen)
    return _FakePopen


def test_spool_and_detach_writes_payload_and_spawns_worker(hook_env, fake_popen):
    raw = b'{"hook_event_name":"SessionEnd","session_id":"s1"}'
    path = hooks.spool_and_detach(raw)

    assert path.parent == hooks.spool_dir(hook_env)
    assert path.read_bytes() == raw
    assert path.suffix == ".json"
    # No .tmp left behind by the atomic write.
    assert not list(path.parent.glob("*.tmp"))

    (args, kw), = fake_popen.calls
    assert args[0] is not None and args[1:4] == ["-m", "sessionator", "_hook_worker"]
    assert args[4] == str(path)
    assert kw["start_new_session"] is True and kw["close_fds"] is True
    for stream in ("stdin", "stdout", "stderr"):
        assert kw[stream] == hooks.subprocess.DEVNULL


def test_spool_names_do_not_collide(hook_env, fake_popen):
    first = hooks.spool_and_detach(b"{}")
    second = hooks.spool_and_detach(b"{}")
    assert first != second


def test_spool_survives_a_failing_spawn(hook_env, monkeypatch):
    def boom(*a, **kw):
        raise OSError("no fork for you")

    monkeypatch.setattr(hooks.subprocess, "Popen", boom)
    path = hooks.spool_and_detach(b'{"hook_event_name":"Stop"}')
    assert path.exists()  # the reap will clear it; the hook still exited clean


# --- `ingest --hook`: silent, exit 0, fast ----------------------------------

def _feed_stdin(monkeypatch, raw: bytes):
    class _Stdin:
        buffer = type("B", (), {"read": staticmethod(lambda: raw)})()

    monkeypatch.setattr(cli.sys, "stdin", _Stdin())


def test_ingest_hook_is_silent_and_exits_zero(hook_env, fake_popen, monkeypatch, capsys):
    _feed_stdin(monkeypatch, b'{"hook_event_name":"SessionEnd"}')
    assert cli.main(["ingest", "--hook"]) == 0
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""
    assert fake_popen.calls


def test_ingest_hook_swallows_every_exception(hook_env, monkeypatch, capsys):
    def boom(_raw):
        raise RuntimeError("data dir is on fire")

    monkeypatch.setattr(hooks, "spool_and_detach", boom)
    _feed_stdin(monkeypatch, b"{}")
    assert cli.main(["ingest", "--hook"]) == 0
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_ingest_hook_never_reaches_argparse(hook_env, fake_popen, monkeypatch, capsys):
    # An unknown flag alongside --hook must not produce argparse's usage text on
    # stderr or a non-zero exit inside the user's session.
    _feed_stdin(monkeypatch, b"{}")
    assert cli.main(["ingest", "--hook", "--not-a-real-flag"]) == 0
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_ingest_hook_returns_promptly(hook_env, fake_popen, monkeypatch):
    _feed_stdin(monkeypatch, b'{"hook_event_name":"PreCompact","trigger":"auto"}')
    start = time.monotonic()
    cli.main(["ingest", "--hook"])
    assert time.monotonic() - start < 0.3


# --- the worker -------------------------------------------------------------

def _claude_lines(n, day="2026-07-01"):
    out = []
    for i in range(n):
        out.append({
            "type": "user",
            "cwd": "/work/proj",
            "timestamp": f"{day}T10:0{i}:00Z",
            "message": {"role": "user", "content": f"ask number {i}"},
        })
        out.append({
            "type": "assistant",
            "message": {
                "role": "assistant", "model": "haiku",
                "content": [{"type": "text", "text": f"answer number {i}"}],
            },
        })
    return out


@pytest.fixture
def worker_env(tmp_path, monkeypatch):
    """A config whose Claude source is a fixture dir, injected into the worker in
    place of the real XDG-loaded one."""
    projects = tmp_path / "projects"
    (projects / "-work-proj").mkdir(parents=True)
    cfg = make_config(tmp_path, claude_dir=projects)
    monkeypatch.setattr("sessionator.config.load", lambda: cfg)
    return cfg, projects


def _spool(cfg, payload) -> Path:
    return hooks.write_spool(cfg, json.dumps(payload).encode())


def test_worker_ingests_the_named_transcript_and_unlinks_the_spool(worker_env):
    cfg, projects = worker_env
    uuid = "aaaa0001-0000-4000-8000-000000000001"
    path = projects / "-work-proj" / f"{uuid}.jsonl"
    path.write_text("\n".join(json.dumps(x) for x in _claude_lines(2)) + "\n")

    spool = _spool(cfg, {
        "hook_event_name": "PreCompact",
        "transcript_path": str(path),
        "trigger": "auto",
        "session_id": uuid,
    })
    hooks.run_worker(spool)

    assert not spool.exists()
    rec = Store(cfg).load()[f"claude/{uuid}"]
    (segment,) = rec.summary_segments
    assert (segment["event"], segment["trigger"]) == ("precompact", "auto")
    assert segment["state"] == "pending"          # no summarizer CLI in tests


def test_worker_falls_back_to_a_watermark_sweep_without_a_transcript(worker_env):
    cfg, projects = worker_env
    uuid = "bbbb0002-0000-4000-8000-000000000002"
    (projects / "-work-proj" / f"{uuid}.jsonl").write_text(
        "\n".join(json.dumps(x) for x in _claude_lines(2)) + "\n"
    )

    # Codex's session_id is fork-shared, so a payload can arrive with no usable
    # transcript path at all; the sweep still finds what changed.
    spool = _spool(cfg, {
        "hook_event_name": "SessionEnd",
        "transcript_path": None,
        "session_id": "shared-thread-id",
        "reason": "other",
    })
    hooks.run_worker(spool)

    assert not spool.exists()
    assert f"claude/{uuid}" in Store(cfg).load()


def test_worker_ignores_a_transcript_that_no_longer_exists(worker_env):
    cfg, projects = worker_env
    spool = _spool(cfg, {
        "hook_event_name": "SessionEnd",
        "transcript_path": str(projects / "-work-proj" / "gone.jsonl"),
    })
    hooks.run_worker(spool)
    assert not spool.exists()
    assert Store(cfg).load() == {}


def test_worker_survives_a_junk_payload(worker_env):
    cfg, _ = worker_env
    spool = hooks.write_spool(cfg, b"{ not json at all")
    hooks.run_worker(spool)
    assert not spool.exists()


def test_concurrent_workers_on_one_session_collapse(worker_env, monkeypatch):
    cfg, projects = worker_env
    uuid = "cccc0003-0000-4000-8000-000000000003"
    path = projects / "-work-proj" / f"{uuid}.jsonl"
    path.write_text("\n".join(json.dumps(x) for x in _claude_lines(2)) + "\n")
    payload = {"hook_event_name": "PreCompact", "transcript_path": str(path)}

    # Hold this session's lock, as an in-flight worker would: the second hook of
    # a PreCompact storm must drop rather than pile up.
    held = hooks._acquire_session_lock(cfg, payload, str(path))
    assert held is not None
    try:
        spool = _spool(cfg, payload)
        hooks.run_worker(spool)
        assert not spool.exists()          # still consumed, never re-run
        assert Store(cfg).load() == {}     # but it did no work
    finally:
        held.release()

    # With the lock free, the same payload is ingested normally.
    hooks.run_worker(_spool(cfg, payload))
    assert f"claude/{uuid}" in Store(cfg).load()


def test_worker_reaps_stale_spool_files(worker_env):
    cfg, _ = worker_env
    stale = hooks.write_spool(cfg, b"{}")
    fresh = hooks.write_spool(cfg, b"{}")
    import os

    old = time.time() - hooks.SPOOL_TTL_SECONDS - 60
    os.utime(stale, (old, old))

    hooks.run_worker(_spool(cfg, {"hook_event_name": "Stop"}))
    assert not stale.exists()
    assert fresh.exists()


def test_reap_ignores_a_missing_directory(tmp_path):
    assert hooks._reap_stale_spool(tmp_path / "nope") == 0


def test_backfill_wait_blocks_on_running_sweep_then_runs(tmp_path, monkeypatch):
    """A hook worker waits for the single-instance lock instead of yielding."""
    from sessionator import summarize
    from sessionator.locking import FileLock
    from conftest import make_config

    cfg = make_config(tmp_path)
    held = FileLock(str(cfg.backfill_lock_path), blocking=False).acquire()
    # Non-waiting call yields immediately.
    assert summarize.backfill(cfg).get("already_running") is True
    # A short wait times out into the same yield, never raises.
    assert summarize.backfill(cfg, wait=0.2).get("already_running") is True
    held.release()
    # With the lock free, the waiting call runs a (work-less) pass.
    stats = summarize.backfill(cfg, wait=0.2)
    assert "already_running" not in stats
