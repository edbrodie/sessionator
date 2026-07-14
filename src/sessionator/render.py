"""Result rendering — the three ``--format`` surfaces (T-002).

* ``compact`` — one scannable row per hit: ``date · harness(model) · cwd-tail ·
  sid · asked``. Falls back to ``(summary pending)`` before the backfill runs.
* ``full`` — every field, with the *actual* file paths and commit SHAs (the old
  engine only showed counts — this closes that gap), the five summary fields, and
  the resume string last.
* ``ndjson`` — the raw records, one JSON object per line, straight from the store.

stdout is data only; a trailing match-count line is diagnostic and goes to
stderr via the CLI, not here.
"""

from __future__ import annotations

import json
import re

from .render_util import cwd_tail, harness_label, trunc
from .resume import resume_string
from .schema import Record, short_sid

_SUMMARY_ORDER = (
    ("asked", "Asked"),
    ("learned", "Learned"),
    ("completed", "Completed"),
    ("left_off", "Left off"),
    ("next_steps", "Next steps"),
)


# ---------------------------------------------------------------------------
# compact
# ---------------------------------------------------------------------------

def render_compact(hits: list[Record]) -> str:
    lines = []
    for rec in hits:
        model = rec.model
        label = harness_label(rec.harness)
        label = f"{label}({model})" if model else label
        asked = (rec.summary or {}).get("asked") or ""
        tail = trunc(asked, 90) if asked else "(summary pending)"
        lines.append(
            "  ".join(
                [
                    rec.date or "??????????",
                    label,
                    cwd_tail(rec.cwd),
                    short_sid(rec.sid),
                    tail,
                ]
            )
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# full
# ---------------------------------------------------------------------------

def render_full(hits: list[Record]) -> str:
    return "\n\n".join(_full_block(rec) for rec in hits)


def _full_block(rec: Record) -> str:
    out = [f"● {rec.sid}"]
    out.append(
        f"  {rec.date} · {harness_label(rec.harness)}"
        + (f" ({rec.model})" if rec.model else "")
        + f" · resolved={rec.resolved or 'unknown'}"
    )
    out.append(f"  cwd:    {rec.cwd or '-'}")
    repo_line = rec.repo or "-"
    if rec.branch:
        repo_line += f" (branch: {rec.branch})"
    out.append(f"  repo:   {repo_line}")
    out.append(f"  active: {rec.last_active or '-'}")
    if rec.forked_from:
        out.append(f"  forked_from: {rec.forked_from}")

    summary = rec.summary or {}
    if any(summary.values()):
        out.append("  summary:")
        for key, label in _SUMMARY_ORDER:
            val = summary.get(key)
            if val:
                out.append(f"    {label}: {_oneline(val)}")
    else:
        out.append(f"  summary: (pending — state {rec.summary_state})")

    kws = rec.keywords or []
    if kws:
        out.append(f"  keywords: {', '.join(str(k) for k in kws)}")
    skills = rec.skills or []
    if skills:
        out.append(f"  skills: {', '.join(str(s) for s in skills)}")

    files = rec.files or []
    out.append(f"  files ({len(files)}):")
    for op, path in files:
        out.append(f"    {op} {path}")
    commits = rec.commits or []
    out.append(f"  commits ({len(commits)}):")
    for sha, subj in commits:
        out.append(f"    {sha} {subj}")

    if rec.prs:
        out.append(f"  prs: {', '.join(str(p) for p in rec.prs)}")
    if rec.subagents:
        out.append(
            "  subagents: "
            + ", ".join(f"{t}×{c}" for t, c in rec.subagents)
        )
    if rec.mcp:
        out.append(f"  mcp: {', '.join(str(m) for m in rec.mcp)}")
    if rec.open_todos:
        out.append("  open_todos:")
        for t in rec.open_todos:
            out.append(f"    - {t}")
    if rec.tests:
        out.append(
            f"  tests: {rec.tests.get('text')} (broken={bool(rec.tests.get('broken'))})"
        )

    out.append(f"  resume: {resume_string(rec)}")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# ndjson
# ---------------------------------------------------------------------------

def render_ndjson(hits: list[Record]) -> str:
    return "\n".join(json.dumps(rec.to_dict(), ensure_ascii=False) for rec in hits)


def _oneline(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()
