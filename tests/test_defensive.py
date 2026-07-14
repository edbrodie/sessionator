"""T-007 defensive-parsing rule checklist.

One test (or a small cluster) per normative rule from the adapter contract,
named ``test_rule<N>_...`` so the coverage bar is the rule set itself rather
than a percentage. Every fixture is synthetic (see fixtures/README.md); no real
transcript content appears here.

Rules (docs/adapter-contract.md §"Defensive parsing"):
  1. Malformed JSONL lines: skip, count in ``parse_warnings``, never raise.
  2. Unknown message/event types: ignore.
  3. Claude filters: sidechains, /subagents/ subtrees, summarizer self-pollution
     (@@S1@@), synthetic (<…>) model ids.
  4. Codex filters: interactive TUI only; sid = top-level id; forked_from
     lineage; history.jsonl never read.
  5. Minimal required fields (sid/harness/date); everything else nullable — a
     record with gaps beats a dropped session.
  6. Compaction/pruning: extract from what is on disk; the excerpt sidecar
     preserves a pre-pruning view for ``show``.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sessionator.adapters import claude, codex
from sessionator.show import render_show
from sessionator.store import Store

from conftest import (
    DEFENSIVE_CLAUDE,
    DEFENSIVE_CODEX,
    DEFENSIVE_CODEX_BOTH,
    make_config,
    make_record,
)

CDIR = DEFENSIVE_CLAUDE / "-home-u-proj"
XDIR = DEFENSIVE_CODEX / "2026" / "07" / "11"


def _cfg(tmp_path):
    return make_config(
        tmp_path, claude_dir=DEFENSIVE_CLAUDE, codex_dir=DEFENSIVE_CODEX
    )


def _claude(tmp_path, stem):
    return claude.extract(CDIR / f"{stem}.jsonl", _cfg(tmp_path))


def _codex(tmp_path, uuid_tag):
    (match,) = list(XDIR.glob(f"rollout-*-{uuid_tag}-*.jsonl"))
    return codex.extract(match, _cfg(tmp_path))


# --- Rule 1: malformed lines -----------------------------------------------

def test_rule1_claude_malformed_line_skipped_and_counted(tmp_path):
    rec = _claude(tmp_path, "d1malform-0000-0000-0000-000000000001")
    assert rec is not None                      # one bad line does not sink it
    assert rec.parse_warnings == 1
    assert "flaky auth test" in rec.excerpt     # the surrounding good turns survive


def test_rule1_codex_malformed_body_line_skipped_and_counted(tmp_path):
    rec = _codex(tmp_path, "019f7001")
    assert rec is not None
    assert rec.parse_warnings == 1
    assert "null-pointer" in rec.excerpt


# --- Rule 2: unknown types ignored -----------------------------------------

def test_rule2_claude_unknown_message_types_ignored(tmp_path):
    # summary / file-history-snapshot / system / a never-before-seen type are
    # interleaved with the real turns — none raise, none count as warnings.
    rec = _claude(tmp_path, "d2unknown-0000-0000-0000-000000000002")
    assert rec is not None
    assert rec.parse_warnings == 0              # unknown != malformed
    assert "Endpoint added" in rec.excerpt
    assert "add the new endpoint" in rec.excerpt


def test_rule2_codex_unknown_payload_types_ignored(tmp_path):
    rec = _codex(tmp_path, "019f7002")
    assert rec is not None
    assert rec.parse_warnings == 0
    assert "10x faster" in rec.excerpt


# --- Rule 3: Claude filters -------------------------------------------------

def test_rule3_claude_summarizer_pollution_filtered(tmp_path):
    # First human turn is one of sessionator's own @@S1@@ batch prompts.
    assert _claude(tmp_path, "d3summ-0000-0000-0000-000000000003") is None


def test_rule3_claude_synthetic_model_id_ignored(tmp_path):
    # A <synthetic> API-retry model must never be recorded; the real one wins.
    rec = _claude(tmp_path, "d3synth-0000-0000-0000-000000000004")
    assert rec is not None
    assert rec.model == "claude-fable-5"


def test_rule3_claude_subagents_subtree_skipped_by_enumerate(tmp_path):
    cfg = _cfg(tmp_path)
    (root,) = claude.discover_sources(cfg)
    files = [p for p, _m, _s in claude.enumerate_sessions(root)]
    assert files                                 # the tree is non-empty
    assert all("subagents" not in Path(p).parts for p in files)


# --- Rule 4: Codex filters --------------------------------------------------

def test_rule4_codex_forked_lineage_captured(tmp_path):
    rec = _codex(tmp_path, "019f7003")
    assert rec is not None
    # sid is keyed on the fork-unique top-level id, NOT the shared session_id.
    assert rec.sid == "codex/019f7003-0000-7000-8000-000000000003"
    assert rec.forked_from == "019f0000-0000-7000-8000-0000000000ff"


def test_rule4_codex_thread_source_not_user_filtered(tmp_path):
    assert _codex(tmp_path, "019f7004") is None


def test_rule4_codex_subagent_originator_filtered(tmp_path):
    # originator != codex-tui (a nested subagent thread) → not an interactive
    # session. (Headless codex_exec is covered separately in test_adapters.)
    assert _codex(tmp_path, "019f7005") is None


def test_rule4_codex_history_jsonl_never_read(tmp_path):
    # A $CODEX_HOME with both a rollout under sessions/ AND a sibling
    # history.jsonl carrying a sentinel: enumerate must yield only the rollout,
    # and the sentinel must appear nowhere in the extracted record.
    cfg = make_config(tmp_path, codex_dir=DEFENSIVE_CODEX_BOTH / "sessions")
    (root,) = codex.discover_sources(cfg)
    files = [p for p, _m, _s in codex.enumerate_sessions(root)]
    assert len(files) == 1
    assert "history.jsonl" not in str(files[0])
    rec = codex.extract(files[0], cfg)
    assert rec is not None
    haystack = rec.excerpt + " ".join(rec.prs)
    assert "SENTINEL-HISTORY-NEVER-READ" not in haystack
    assert "pull/999" not in haystack


# --- Rule 5: minimal fields, everything else nullable -----------------------

def test_rule5_claude_no_cwd_uses_folder_fallback(tmp_path):
    rec = claude.extract(
        DEFENSIVE_CLAUDE / "-home-u-nocwd"
        / "d5nocwd-0000-0000-0000-000000000005.jsonl",
        _cfg(tmp_path),
    )
    assert rec is not None
    assert rec.cwd == "/home/u/nocwd"           # decoded from the mangled dir


def test_rule5_claude_no_model_still_yields_record(tmp_path):
    rec = _claude(tmp_path, "d5nomodel-0000-0000-0000-000000000006")
    assert rec is not None
    assert rec.model is None                     # gap, not a drop


def test_rule5_claude_no_timestamp_falls_back_to_mtime_day(tmp_path):
    path = CDIR / "d5nots-0000-0000-0000-000000000007.jsonl"
    rec = claude.extract(path, _cfg(tmp_path))
    assert rec is not None
    expected = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")
    assert rec.date == expected


def test_rule5_codex_no_model_still_yields_record(tmp_path):
    rec = _codex(tmp_path, "019f7006")
    assert rec is not None
    assert rec.model is None


def test_rule5_codex_no_cwd_leaves_cwd_empty(tmp_path):
    rec = _codex(tmp_path, "019f7007")
    assert rec is not None
    assert rec.cwd == ""                         # no folder fallback for codex


# --- Rule 6: compaction / pruning ------------------------------------------

def test_rule6_claude_precompact_truncation_still_extracts(tmp_path):
    # File opens mid-stream with a PreCompact summary record (the on-disk state
    # after compaction). Extraction works from what is present.
    rec = _claude(tmp_path, "d6precompact-0000-0000-0000-000000000008")
    assert rec is not None
    assert "Resuming the migration" in rec.excerpt


def test_rule6_show_falls_back_to_excerpt_sidecar_when_transcript_pruned(tmp_path):
    # The harness pruned the live transcript; the pruning-proof sidecar backs it.
    cfg = make_config(tmp_path)
    store = Store(cfg)
    rec = make_record("claude/pruned1", cwd="/home/ed/proj")
    rec.excerpt = "USER: the pre-pruning conversation we want to keep"
    rec.excerpt_path = store.write_excerpt(rec)
    rec.transcript_path = str(tmp_path / "gone" / "pruned1.jsonl")  # never existed
    out = render_show(store, rec, tail=4000)
    assert "[excerpt sidecar]" in out
    assert "pre-pruning conversation we want to keep" in out


def test_rule6_show_reports_no_transcript_when_nothing_on_disk(tmp_path):
    cfg = make_config(tmp_path)
    store = Store(cfg)
    rec = make_record("claude/bare1", cwd="/home/ed/proj")  # no excerpt sidecar
    rec.transcript_path = str(tmp_path / "gone" / "bare1.jsonl")
    out = render_show(store, rec, tail=4000)
    assert "[no transcript available]" in out
