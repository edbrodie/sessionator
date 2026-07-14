"""Session-record schema v1.

The record is the single unit stored per session (one JSON object per line in
``store.jsonl``). This module owns the dataclass, the schema version, the
uniform session-key (``sid``) helpers, and validation. It has no I/O and no
dependencies beyond the standard library.

The field set and their semantics are fixed by the T-001 resolution. Three
fields are operational additions the runtime/adapter designs require and T-001
does not enumerate — they are additive and nullable/zero-valued so a v1 reader
that ignores them still sees a conformant record:

* ``summary_state`` — ``pending`` | ``stale`` | ``done`` | ``error``; drives the
  backfill work-set and the ``status`` "pending summaries" count (T-005).
* ``parse_warnings`` — count of malformed lines skipped while parsing (T-007
  defensive rule 1).
* ``forked_from`` — Codex fork lineage (``forked_from_id``), captured per T-007
  defensive rule 4; ``None`` for Claude and non-forked Codex sessions.
"""

from __future__ import annotations

from dataclasses import dataclass, field

SCHEMA_VERSION = 1

HARNESSES = ("claude", "codex")

# The five named summary fields (T-001). Order is the presentation order.
SUMMARY_FIELDS = ("asked", "learned", "completed", "left_off", "next_steps")

RESOLVED_VALUES = ("open", "done", "unknown")

SUMMARY_STATES = ("pending", "stale", "done", "error")


def empty_summary() -> dict:
    """A fresh summary object with every field the empty string."""
    return {k: "" for k in SUMMARY_FIELDS}


def make_sid(harness: str, uuid: str) -> str:
    """Compose the uniform session key ``"<harness>/<uuid>"``."""
    return f"{harness}/{uuid}"


def split_sid(sid: str) -> tuple[str, str]:
    """Split a sid into ``(harness, uuid)``. Raises ``ValueError`` if malformed."""
    harness, sep, uuid = sid.partition("/")
    if not sep or not harness or not uuid:
        raise ValueError(f"malformed sid: {sid!r}")
    return harness, uuid


def sid_uuid(sid: str) -> str:
    """The uuid component of a sid — the anchor/display root."""
    return split_sid(sid)[1]


def short_sid(sid: str, width: int = 8) -> str:
    """Truncated display form, e.g. ``claude/b4ae266f``. Truncates the uuid only,
    so the harness prefix is always intact."""
    harness, uuid = split_sid(sid)
    return f"{harness}/{uuid[:width]}"


# Persisted field order (matches the T-001 schema listing for readable JSONL).
_PERSIST_FIELDS = (
    "schema_version",
    "sid",
    "harness",
    "native_id",
    "forked_from",
    "model",
    "date",
    "last_active",
    "indexed_at",
    "cwd",
    "repo",
    "branch",
    "files",
    "commits",
    "prs",
    "keywords",
    "skills",
    "subagents",
    "mcp",
    "open_todos",
    "tests",
    "resolved",
    "summary",
    "transcript_path",
    "excerpt_path",
    "summary_state",
    "parse_warnings",
)


@dataclass
class Record:
    """One session, schema v1. ``excerpt`` is transient (the capped, private-
    stripped USER/ASSISTANT text) — it is written to a sidecar by the store and
    never serialized into the record itself."""

    sid: str
    harness: str
    native_id: str
    date: str
    cwd: str = ""
    last_active: str = ""
    indexed_at: str = ""
    model: str | None = None
    repo: str | None = None
    branch: str | None = None
    forked_from: str | None = None
    files: list = field(default_factory=list)
    commits: list = field(default_factory=list)
    prs: list = field(default_factory=list)
    keywords: list = field(default_factory=list)
    skills: list = field(default_factory=list)
    subagents: list = field(default_factory=list)
    mcp: list = field(default_factory=list)
    open_todos: list = field(default_factory=list)
    tests: dict | None = None
    resolved: str = "unknown"
    summary: dict = field(default_factory=empty_summary)
    transcript_path: str = ""
    excerpt_path: str | None = None
    summary_state: str = "pending"
    parse_warnings: int = 0
    schema_version: int = SCHEMA_VERSION

    # Transient — never serialized (see to_dict).
    excerpt: str = ""

    def to_dict(self) -> dict:
        """Serializable dict in canonical field order, excluding the transient
        ``excerpt``."""
        out = {}
        for name in _PERSIST_FIELDS:
            out[name] = getattr(self, name)
        return out

    @classmethod
    def from_dict(cls, d: dict) -> "Record":
        """Rebuild a Record from a stored dict, tolerating missing optional
        fields (forward/backward compatibility)."""
        summary = d.get("summary") or {}
        merged_summary = empty_summary()
        if isinstance(summary, dict):
            for k in SUMMARY_FIELDS:
                v = summary.get(k)
                if isinstance(v, str):
                    merged_summary[k] = v
        return cls(
            sid=d["sid"],
            harness=d["harness"],
            native_id=d.get("native_id", ""),
            date=d.get("date", ""),
            cwd=d.get("cwd", ""),
            last_active=d.get("last_active", ""),
            indexed_at=d.get("indexed_at", ""),
            model=d.get("model"),
            repo=d.get("repo"),
            branch=d.get("branch"),
            forked_from=d.get("forked_from"),
            files=d.get("files") or [],
            commits=d.get("commits") or [],
            prs=d.get("prs") or [],
            keywords=d.get("keywords") or [],
            skills=d.get("skills") or [],
            subagents=d.get("subagents") or [],
            mcp=d.get("mcp") or [],
            open_todos=d.get("open_todos") or [],
            tests=d.get("tests"),
            resolved=d.get("resolved", "unknown"),
            summary=merged_summary,
            transcript_path=d.get("transcript_path", ""),
            excerpt_path=d.get("excerpt_path"),
            summary_state=d.get("summary_state", "pending"),
            parse_warnings=int(d.get("parse_warnings") or 0),
            schema_version=int(d.get("schema_version") or SCHEMA_VERSION),
        )


def validate(rec: Record) -> list[str]:
    """Return a list of schema problems (empty = valid). Minimal required set is
    sid/harness/date (T-007 rule 5); everything else is best-effort nullable."""
    problems = []
    try:
        harness, uuid = split_sid(rec.sid)
    except ValueError as e:
        problems.append(str(e))
        harness = uuid = None
    if rec.harness not in HARNESSES:
        problems.append(f"unknown harness: {rec.harness!r}")
    if harness is not None and harness != rec.harness:
        problems.append(f"sid harness {harness!r} != record harness {rec.harness!r}")
    if not rec.date:
        problems.append("missing date")
    if rec.resolved not in RESOLVED_VALUES:
        problems.append(f"bad resolved: {rec.resolved!r}")
    if rec.summary_state not in SUMMARY_STATES:
        problems.append(f"bad summary_state: {rec.summary_state!r}")
    if set(rec.summary or {}) != set(SUMMARY_FIELDS):
        problems.append("summary must have exactly the five named fields")
    return problems
