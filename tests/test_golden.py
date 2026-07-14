"""Golden-output tests: rendering + resume + exit codes pinned against a small
controlled store built by reconciling the e2e fixture corpus.

The corpus (fixtures/e2e/) has three interactive sessions with stable far-past
dates, so recency ordering and every rendered date are deterministic regardless
of when the suite runs:

    2020-03-15  claude/c1a0d001  auth fix   — commit ab12345, PR /42, done
    2020-02-10  codex/019fc0de   parser     — commit cd67890, PR /43, forked, done
    2020-01-05  claude/c1a0d002  dashboard  — one Write, unknown

Summaries are never generated in tests (no CLI), so compact rows pin the
``(summary pending)`` fallback. ``last_active``/``indexed_at`` carry a machine
timezone and a wall-clock stamp, so those two fields are asserted structurally,
not byte-pinned; everything else is exact.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from sessionator import cli
from sessionator.reconcile import reconcile
from sessionator.render import render_compact, render_full, render_ndjson
from sessionator.search import Filters, search
from sessionator.store import Store

from conftest import E2E_CLAUDE_HOME, E2E_CODEX_HOME, e2e_config

# A fixed "today" well after every fixture date pins recency decay.
TODAY = date(2020, 6, 1)

SID_AUTH = "claude/c1a0d001-0000-4000-8000-000000000001"
SID_PARSER = "codex/019fc0de-0003-7000-8000-000000000003"
SID_DASH = "claude/c1a0d002-0000-4000-8000-000000000002"

EXPECTED_COMPACT = (
    "2020-03-15  Claude(claude-fable-5)  ed/proj  claude/c1a0d001  (summary pending)\n"
    "2020-02-10  Codex(gpt-5.6-luna)  ed/api  codex/019fc0de  (summary pending)\n"
    "2020-01-05  Claude(opus-4.8)  ed/proj  claude/c1a0d002  (summary pending)"
)

RESUME = {
    SID_AUTH: "cd /home/ed/proj && claude --resume c1a0d001-0000-4000-8000-000000000001",
    SID_PARSER: "cd /home/ed/api && codex resume 019fc0de-0003-7000-8000-000000000003",
    SID_DASH: "cd /home/ed/proj && claude --resume c1a0d002-0000-4000-8000-000000000002",
}


@pytest.fixture
def golden_store(tmp_path):
    """Reconcile the e2e corpus into a temp store; return (cfg, records)."""
    cfg = e2e_config(tmp_path)
    result = reconcile(cfg, kick_backfill=False)
    assert result.upserted_new == 3
    assert result.by_harness == {"claude": 2, "codex": 1}
    records = Store(cfg).load()
    assert set(records) == {SID_AUTH, SID_PARSER, SID_DASH}
    return cfg, records


def _recency(cfg, records):
    return search(cfg, [], Filters(), limit=0, records=records, _today=TODAY)


# --- compact ---------------------------------------------------------------

def test_golden_compact_full_store(golden_store):
    cfg, records = golden_store
    hits = _recency(cfg, records)
    assert render_compact(hits) == EXPECTED_COMPACT


# --- resume ----------------------------------------------------------------

def test_golden_resume_strings_exact(golden_store):
    _cfg, records = golden_store
    from sessionator.resume import resume_string

    for sid, expected in RESUME.items():
        assert resume_string(records[sid]) == expected


# --- full ------------------------------------------------------------------

def test_golden_full_pins_deterministic_lines(golden_store):
    cfg, records = golden_store
    out = render_full(_recency(cfg, records))

    # Header + deterministic evidence for the auth session.
    assert f"● {SID_AUTH}" in out
    assert "2020-03-15 · Claude (claude-fable-5) · resolved=done" in out
    assert "cwd:    /home/ed/proj" in out
    assert "ab12345 fix auth bug" in out            # SHA + subject, not a count
    assert "M auth.py" in out
    assert "https://github.com/acme/proj/pull/42" in out
    assert "tests: 3 passed (broken=False)" in out
    # Codex fork lineage surfaces.
    assert "forked_from: 019f0000-0003-7000-8000-0000000000aa" in out
    assert "cd67890 refactor parser" in out
    # Each block ends on its resume line.
    for sid in (SID_AUTH, SID_PARSER, SID_DASH):
        assert RESUME[sid] in out


# --- ndjson ----------------------------------------------------------------

def test_golden_ndjson_fields_and_order(golden_store):
    cfg, records = golden_store
    lines = render_ndjson(_recency(cfg, records)).splitlines()
    parsed = [json.loads(ln) for ln in lines]

    assert [d["sid"] for d in parsed] == [SID_AUTH, SID_PARSER, SID_DASH]

    by_sid = {d["sid"]: d for d in parsed}
    auth = by_sid[SID_AUTH]
    assert auth["date"] == "2020-03-15"
    assert auth["resolved"] == "done"
    assert auth["commits"] == [["ab12345", "fix auth bug"]]
    assert auth["prs"] == ["https://github.com/acme/proj/pull/42"]
    assert auth["forked_from"] is None

    parser = by_sid[SID_PARSER]
    assert parser["forked_from"] == "019f0000-0003-7000-8000-0000000000aa"
    assert parser["model"] == "gpt-5.6-luna"

    # No fabricated private span survives into the serialized record anywhere.
    blob = render_ndjson(_recency(cfg, records))
    assert "sk-fake-e2e-001" not in blob
    assert "tok-fake-e2e-003" not in blob


# --- exit codes (grep semantics) pinned end-to-end via cli.main ------------

@pytest.fixture
def e2e_cli(tmp_path, monkeypatch):
    """Point the real CLI's config/data at temp dirs and its sources at the e2e
    corpus, then ingest it (no backfill spawn). Yields nothing; tests call
    ``cli.main`` and read exit codes / stdout."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(E2E_CLAUDE_HOME))
    monkeypatch.setenv("CODEX_HOME", str(E2E_CODEX_HOME))
    monkeypatch.setenv("PATH", "")  # no CLI detected → no summarizer anywhere
    assert cli.main(["ingest", "--no-backfill"]) == 0


def test_golden_exit_0_on_hit(e2e_cli, capsys):
    code = cli.main(["parser", "--no-reconcile"])
    out = capsys.readouterr()
    assert code == 0
    assert "codex/019fc0de" in out.out


def test_golden_exit_1_on_zero_hits(e2e_cli, capsys):
    assert cli.main(["nonexistent-zzz", "--no-reconcile"]) == 1


def test_golden_exit_2_on_ambiguous_show(e2e_cli, capsys):
    # Both claude uuids share the c1a0d00… prefix → ambiguous.
    code = cli.main(["show", "claude/c1a0d00", "--no-reconcile"])
    err = capsys.readouterr().err
    assert code == 2
    assert "ambiguous" in err


def test_golden_resume_one_line_via_cli(e2e_cli, capsys):
    code = cli.main(["resume", "codex/019fc0de", "--no-reconcile"])
    out = capsys.readouterr()
    assert code == 0
    assert out.out.strip() == RESUME[SID_PARSER]
