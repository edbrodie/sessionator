from sessionator import summarize


def test_build_prompt_has_markers():
    chunk = [("claude/a", "USER: hi\n\nASSISTANT: done"), ("codex/b", "USER: yo")]
    prompt = summarize.build_prompt(chunk)
    assert "@@S1@@" in prompt
    assert "@@S2@@" in prompt
    assert "**Next steps:**" in prompt


def test_split_and_parse_roundtrip():
    raw = (
        "@@S1@@\n"
        "- **Asked:** fix the auth bug\n"
        "- **Learned:** the token was double-encoded\n"
        "- **Completed:** patched the handler and pushed\n"
        "- **Left off:** green build on main\n"
        "- **Next steps:** None\n"
        "- **Resolved:** done\n"
        "@@S2@@\n"
        "- **Asked:** refactor parser\n"
        "- **Learned:** \n"
        "- **Completed:** split the tokenizer\n"
        "- **Left off:** tests still red\n"
        "- **Next steps:** fix the failing case\n"
        "- **Resolved:** open\n"
    )
    per = summarize._split_by_marker(raw)
    assert set(per) == {"S1", "S2"}

    f1 = summarize._parse_fields(per["S1"])
    assert f1["summary"]["asked"] == "fix the auth bug"
    assert f1["summary"]["learned"] == "the token was double-encoded"
    assert f1["summary"]["next_steps"] == "None"
    assert f1["resolved"] == "done"

    f2 = summarize._parse_fields(per["S2"])
    assert f2["summary"]["learned"] == ""  # blank field tolerated
    assert f2["resolved"] == "open"


def test_parse_fields_rejects_bad_resolved():
    body = "- **Asked:** something\n- **Resolved:** maybe\n"
    fields = summarize._parse_fields(body)
    assert "resolved" not in fields  # invalid enum dropped
    assert fields["summary"]["asked"] == "something"


def test_parse_fields_none_when_empty():
    assert summarize._parse_fields("no labels at all") is None


def test_cap_excerpt():
    text = "x" * 20000
    capped = summarize._cap_excerpt(text)
    assert len(capped) < len(text)
    assert "[trimmed]" in capped


def test_prompt_carries_sentinel_and_is_detected_as_pollution():
    from sessionator.adapters._common import SUMMARIZER_SENTINEL, Walker

    prompt = summarize.build_prompt([("claude/a", "USER: hi")])
    assert prompt.startswith(SUMMARIZER_SENTINEL)

    # A transcript whose first human turn is our own prompt is self-pollution.
    w = Walker()
    w.add_user(prompt)
    assert w.is_summarizer_pollution() is True
    assert w.finish(fallback_day="2026-01-01") is None

    # A real session is not.
    w2 = Walker()
    w2.add_user("fix the flaky auth test")
    assert w2.is_summarizer_pollution() is False


# --- segment rollups --------------------------------------------------------

import json  # noqa: E402

import pytest  # noqa: E402

from sessionator import cli, segments as seg  # noqa: E402
from sessionator.store import Store  # noqa: E402

from conftest import make_config, make_record  # noqa: E402

ROLLUP_REPLY = (
    "@@S1@@\n"
    "- **Asked:** ship the parser\n"
    "- **Learned:** the tokenizer was the bottleneck\n"
    "- **Completed:** split the tokenizer\n"
    "- **Left off:** tests green\n"
    "- **Next steps:** None\n"
    "- **Resolved:** done\n"
)

EXCERPT = (
    "USER: first ask\n\nASSISTANT: first answer\n\n"
    "USER: second ask\n\nASSISTANT: second answer"
)


def test_build_rollup_prompt_shape():
    from sessionator.adapters._common import SUMMARIZER_SENTINEL

    prev = {
        "asked": "ship the parser", "learned": "", "completed": "started",
        "left_off": "mid-refactor", "next_steps": "finish it",
    }
    prompt = summarize.build_rollup_prompt(prev, "USER: keep going", "precompact")
    assert prompt.startswith(SUMMARIZER_SENTINEL)
    assert "@@S1@@" in prompt
    assert "- **Asked:** ship the parser" in prompt      # current summary echoed
    assert "**Next steps:**" in prompt                    # same six labels
    assert "cut at: precompact" in prompt
    assert "USER: keep going" in prompt

    # No summary yet is said explicitly, not left blank.
    assert "(none yet)" in summarize.build_rollup_prompt(None, "USER: hi", None)


def _segmented_cfg(tmp_path, *, harness="claude", cli_path="/nonexistent/claude"):
    cfg = make_config(tmp_path)
    if cli_path:
        cfg.sources[harness].cli = cli_path
    store = Store(cfg)
    rec = make_record(f"{harness}/seg1", harness=harness)
    rec.excerpt = EXCERPT
    rec.excerpt_path = store.write_excerpt(rec)
    segment = seg.append_segment(
        rec, event="precompact", trigger="auto", turn_count=4, size=1234,
    )
    store.write_segment_excerpt(rec, segment["seq"], EXCERPT)
    rec.summary_state = "pending"
    store.write({rec.sid: rec})
    return cfg, store, rec.sid


def test_select_work_prefers_the_segment_sidecar(tmp_path):
    cfg, store, sid = _segmented_cfg(tmp_path)
    # Overwrite the sidecar so a preference for it is observable.
    rec = store.load()[sid]
    store.write_segment_excerpt(rec, 1, "USER: sidecar text only")
    work = summarize._select_work(cfg, store)
    assert len(work) == 1
    assert work[0].seq == 1
    assert work[0].text.strip() == "USER: sidecar text only"

    # Without a sidecar it falls back to slicing the main excerpt.
    store.segment_excerpt_path_for(sid, 1).unlink()
    work = summarize._select_work(cfg, store)
    assert work[0].text == EXCERPT


def test_select_work_skips_other_sids_with_only_sid(tmp_path):
    cfg, store, sid = _segmented_cfg(tmp_path)
    records = store.load()
    other = make_record("claude/legacy2")
    other.excerpt = "USER: unrelated"
    other.excerpt_path = store.write_excerpt(other)
    records[other.sid] = other
    store.write(records)

    assert {w.sid for w in summarize._select_work(cfg, store)} == {sid, "claude/legacy2"}
    assert {w.sid for w in summarize._select_work(cfg, store, only_sid=sid)} == {sid}
    # Segment work is ordered first.
    assert summarize._select_work(cfg, store)[0].seq == 1


def test_backfill_segment_rolls_up_and_marks_done(tmp_path, monkeypatch):
    cfg, store, sid = _segmented_cfg(tmp_path)
    seen = []

    def fake_invoke(config, cli_harness, cli_path, prompt):
        seen.append(prompt)
        return ROLLUP_REPLY

    monkeypatch.setattr(summarize, "_invoke_cli", fake_invoke)
    stats = summarize.backfill(cfg, only_sid=sid)
    assert stats["segments"] == 1
    assert stats["updated"] == 1
    assert len(seen) == 1

    rec = store.load()[sid]
    assert rec.summary["asked"] == "ship the parser"
    assert rec.resolved == "done"
    assert rec.summary_state == "done"
    segment = rec.summary_segments[0]
    assert segment["state"] == "done"
    assert segment["summary"]["completed"] == "split the tokenizer"


def test_backfill_segment_failure_leaves_error_and_keeps_summary(tmp_path, monkeypatch):
    cfg, store, sid = _segmented_cfg(tmp_path)
    monkeypatch.setattr(summarize, "_invoke_cli", lambda *a, **k: None)
    stats = summarize.backfill(cfg, only_sid=sid)
    assert stats["updated"] == 0 and stats["errors"] == 1
    rec = store.load()[sid]
    assert rec.summary_segments[0]["state"] == "error"
    assert rec.summary_state == "error"


def test_backfill_without_cli_leaves_segment_pending(tmp_path):
    cfg, store, sid = _segmented_cfg(tmp_path, cli_path=None)
    stats = summarize.backfill(cfg, only_sid=sid)
    assert stats["skipped_no_cli"] == 1
    rec = store.load()[sid]
    assert rec.summary_segments[0]["state"] == "pending"
    assert rec.summary_state == "pending"


def test_backfill_threads_the_rollup_through_successive_segments(tmp_path, monkeypatch):
    cfg, store, sid = _segmented_cfg(tmp_path)
    records = store.load()
    rec = records[sid]
    second = seg.append_segment(
        rec, event="session_end", turn_count=4, size=2000, force=True,
    )
    store.write_segment_excerpt(rec, second["seq"], "USER: the tail")
    store.write(records)

    prompts = []

    def fake_invoke(config, cli_harness, cli_path, prompt):
        prompts.append(prompt)
        n = len(prompts)
        return ROLLUP_REPLY.replace("ship the parser", f"round {n}")

    monkeypatch.setattr(summarize, "_invoke_cli", fake_invoke)
    summarize.backfill(cfg, only_sid=sid)
    # The second call was given the first call's result as the running summary.
    assert "round 1" in prompts[1]
    rec = store.load()[sid]
    assert rec.summary["asked"] == "round 2"
    assert [s["state"] for s in rec.summary_segments] == ["done", "done"]


def test_apply_ignores_a_segment_someone_else_resolved(tmp_path):
    cfg, store, sid = _segmented_cfg(tmp_path)
    records = store.load()
    seg.mark_done(records[sid], 1, {"asked": "already handled"})
    store.write(records)

    fields = {"summary": {k: "late" for k in ("asked", "learned", "completed", "left_off", "next_steps")}}
    updated = summarize._apply(
        cfg, store, {}, set(), segment_results={(sid, 1): fields}, segment_errors=set(),
    )
    assert updated == 0
    assert store.load()[sid].summary["asked"] != "late"


def test_prompt_travels_on_stdin_not_argv(monkeypatch):
    from pathlib import Path
    """Session excerpts must never appear in the process list."""
    import subprocess
    from sessionator import summarize
    from sessionator.config import Config, Source

    seen = []

    class _P:
        returncode = 0
        stdout = "@@S1@@\n- **Asked:** x\n"

    def fake_run(args, **kw):
        seen.append((list(args), kw.get("input")))
        return _P()

    monkeypatch.setattr(subprocess, "run", fake_run)
    cfg = Config(
        data_dir=Path("/nonexistent"),
        exclusions=[],
        sources={"claude": Source(name="claude", enabled=True, transcript_dir="", cli="/x/claude"),
                 "codex": Source(name="codex", enabled=True, transcript_dir="", cli="/x/codex")},
        summarize={"claude": {"model": "haiku"}, "codex": {"model": "m", "reasoning": "low"}},
        path=Path("/nonexistent/config.toml"),
    )
    secret = "USER: the private thing"
    assert summarize._invoke_claude(cfg, "/x/claude", secret)
    assert summarize._invoke_codex(cfg, "/x/codex", secret)
    for args, stdin_text in seen:
        assert secret not in " ".join(args)
        assert stdin_text == secret
