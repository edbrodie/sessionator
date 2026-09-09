"""Shared test helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from sessionator.config import DEFAULT_SUMMARIZE, Config, Source
from sessionator.schema import Record, empty_summary, split_sid
from sessionator.store import Store

FIXTURES = Path(__file__).parent / "fixtures"

# Defensive-parsing corpus (T-007 rules), kept in dirs the reconcile tests do
# not scan so their record counts stay pinned. See fixtures/README.md.
DEFENSIVE = FIXTURES / "defensive"
DEFENSIVE_CLAUDE = DEFENSIVE / "claude"
DEFENSIVE_CODEX = DEFENSIVE / "codex"
DEFENSIVE_CODEX_BOTH = DEFENSIVE / "codex_both"

# End-to-end / golden corpus in real harness dir layout (CLAUDE_CONFIG_DIR /
# CODEX_HOME point at the *-home roots; the transcript dirs are their
# projects/ and sessions/ children). Stable far-past dates keep recency
# ordering deterministic regardless of the wall clock.
E2E = FIXTURES / "e2e"
E2E_CLAUDE_HOME = E2E / "claude-home"
E2E_CODEX_HOME = E2E / "codex-home"


def e2e_config(tmp_path):
    """A Config wired to the e2e golden corpus and a temp data dir (no CLIs)."""
    return make_config(
        tmp_path,
        claude_dir=E2E_CLAUDE_HOME / "projects",
        codex_dir=E2E_CODEX_HOME / "sessions",
    )


def make_record(
    sid,
    *,
    date="2026-07-01",
    harness=None,
    cwd="/home/ed/proj",
    repo=None,
    branch=None,
    model="test-model",
    resolved="unknown",
    keywords=(),
    skills=(),
    commits=(),
    files=(),
    summary=None,
    summary_state="pending",
    native_id=None,
    last_active=None,
    indexed_at="2026-07-01T00:00:00",
):
    """Build a schema-conformant Record for search/index/render tests."""
    h = harness or split_sid(sid)[0]
    s = empty_summary()
    if summary:
        s.update(summary)
    return Record(
        sid=sid,
        harness=h,
        native_id=native_id or split_sid(sid)[1],
        date=date,
        cwd=cwd,
        last_active=last_active or f"{date}T00:00:00",
        indexed_at=indexed_at,
        model=model,
        repo=repo,
        branch=branch,
        resolved=resolved,
        keywords=list(keywords),
        skills=list(skills),
        commits=[list(c) for c in commits],
        files=[list(f) for f in files],
        summary=s,
        summary_state=summary_state,
    )


def seed_store(cfg, records):
    """Write ``records`` (an iterable of Record) into the config's store."""
    store = Store(cfg)
    store.write({r.sid: r for r in records})
    return store


def make_config(tmp_path, *, claude_dir=None, codex_dir=None, exclusions=()):
    """A Config wired to fixture transcript dirs and a temp data dir. CLIs are
    None so no summarizer runs in tests."""
    sources = {
        "claude": Source(
            name="claude",
            enabled=claude_dir is not None,
            transcript_dir=str(claude_dir) if claude_dir else "",
            cli=None,
        ),
        "codex": Source(
            name="codex",
            enabled=codex_dir is not None,
            transcript_dir=str(codex_dir) if codex_dir else "",
            cli=None,
        ),
    }
    return Config(
        data_dir=Path(tmp_path) / "data",
        exclusions=list(exclusions),
        sources=sources,
        summarize={k: dict(v) for k, v in DEFAULT_SUMMARIZE.items()},
        path=Path(tmp_path) / "config.toml",
    )


@pytest.fixture
def claude_fixtures():
    return FIXTURES / "claude"


@pytest.fixture
def codex_fixtures():
    return FIXTURES / "codex"


@pytest.fixture
def codex_headless_fixtures():
    return FIXTURES / "codex_headless"


@pytest.fixture
def codex_desktop_fixtures():
    return FIXTURES / "codex_desktop"
