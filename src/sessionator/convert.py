"""One-time legacy-store migration into schema v1.

Reads the pre-sessionator store — the old daily-report engine's
``~/claude-daily-reports/.store/sessions.jsonl`` — and folds each record into the
sessionator store two ways:

* **MERGE** — where a schema-v1 record already exists for the same session (the
  new ingest already scanned that live transcript), graft the old 3-bullet
  summary (parsed into the five named fields), the old ``resolved``, and the old
  keywords onto the new record, but only while the new record's summary is still
  unwritten (``summary_state`` in pending/stale/error). A new record that already
  carries a real summary (``done``) is left untouched and counted skipped.
* **APPEND** — where no v1 record exists (the live transcript was pruned before
  the new tool first ran), convert the old record into a full schema-v1 Record
  and add it. No excerpt sidecar is written — the source text is gone — so
  ``excerpt_path`` is ``None`` and ``transcript_path`` is carried over verbatim
  (it may be dead).

The old→new summary mapping is fixed by T-001: ``Asked→asked``, ``Done→completed``,
``Left off→left_off``; ``learned`` and ``next_steps`` are left empty (the old
engine never produced them).

**Idempotent.** A second run finds every merge target already ``done`` (skipped)
and every append already present (skipped), so it grafts nothing new.

**Privacy.** Every converted record passes the same gates as live ingest —
``cwd_excluded`` drops vault sessions, ``scrub_files`` redacts excluded paths in an
appended record's manifest, ``strip_private`` elides ``<private>`` spans from the
grafted summary — and a tombstoned sid is never re-appended.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .locking import FileLock
from .privacy import cwd_excluded, scrub_files, strip_private
from .schema import RESOLVED_VALUES, Record, empty_summary, make_sid
from .store import Store

# The old engine's default store location.
DEFAULT_LEGACY_STORE = Path.home() / "claude-daily-reports" / ".store" / "sessions.jsonl"

# States whose summary is not yet authored — a graft target. ``done`` is left
# alone (a real summary already beats the legacy 3-bullet).
_GRAFTABLE_STATES = ("pending", "stale", "error")

# A 3-bullet summary line: ``- **Label:** text``.
_BULLET_RX = re.compile(r"^\s*-\s*\*\*(?P<label>[^:*]+):\*\*\s*(?P<text>.*)$")

# Old bullet label (lowercased) → schema-v1 summary field.
_LABEL_MAP = {
    "asked": "asked",
    "done": "completed",
    "left off": "left_off",
    "left-off": "left_off",
}

# The old engine's "no summary" sentinel — never graft it as content.
_PLACEHOLDER_RX = re.compile(r"_\(summary unavailable\)_")


@dataclass
class ConvertResult:
    read: int = 0
    merged: int = 0
    appended: int = 0
    skipped: int = 0
    excluded: int = 0
    tombstoned: int = 0
    unknown_source: int = 0
    parse_errors: int = 0


def parse_summary(blob) -> dict:
    """Parse an old 3-bullet markdown summary into the five named v1 fields.

    Line-oriented and wrap-tolerant: a bullet header opens a field and any
    following non-header lines append to it, so a summary the old engine wrapped
    across lines still round-trips. ``learned``/``next_steps`` stay empty (the old
    schema had no equivalent). The ``_(summary unavailable)_`` placeholder yields
    an empty field, not literal text."""
    out = empty_summary()
    if not isinstance(blob, str) or not blob.strip():
        return out
    current = None
    for raw in blob.splitlines():
        m = _BULLET_RX.match(raw)
        if m:
            field = _LABEL_MAP.get(m.group("label").strip().lower())
            if field:
                current = field
                out[current] = m.group("text").strip()
            else:
                current = None  # an unrecognized bullet ends the previous field
            continue
        if current:
            extra = raw.strip()
            if extra:
                out[current] = f"{out[current]} {extra}".strip()
    for k, v in out.items():
        if _PLACEHOLDER_RX.search(v):
            out[k] = ""
    return out


def _codex_uuid(session_id: str) -> str:
    """The fork/session uuid from an old codex ``session_id`` of the form
    ``rollout-<ISO-ts>-<uuid>`` — the 5-group tail, matching the codex adapter's
    filename fallback so an old codex record keys onto its v1 twin."""
    stem = session_id
    if stem.startswith("rollout-"):
        stem = stem[len("rollout-"):]
    parts = stem.split("-")
    return "-".join(parts[-5:]) if len(parts) >= 5 else stem


def _legacy_key(d: dict) -> tuple[str, str] | None:
    """``(harness, uuid)`` for an old record, or ``None`` for an unknown source.
    Claude: the raw ``session_id`` uuid. Codex: the rollout-name uuid tail."""
    src = d.get("source")
    sid = d.get("session_id") or ""
    if not sid:
        return None
    if src == "claude":
        return "claude", sid
    if src == "codex":
        return "codex", _codex_uuid(sid)
    return None


def _union(base, extra) -> list:
    """Order-preserving union: ``base`` first, then ``extra`` items not present."""
    out = list(base or [])
    seen = set(out)
    for item in extra or []:
        if item not in seen:
            out.append(item)
            seen.add(item)
    return out


def _to_record(d: dict, harness: str, uuid: str, now: str, exclusions) -> Record:
    """Build a full schema-v1 Record from an old record (the APPEND path)."""
    summary = {k: strip_private(v) for k, v in parse_summary(d.get("summary")).items()}
    state = "done" if any(summary.values()) else "pending"
    resolved = d.get("resolved")
    if resolved not in RESOLVED_VALUES:
        resolved = "unknown"
    files, _ = scrub_files(list(d.get("files") or []), exclusions)
    return Record(
        sid=make_sid(harness, uuid),
        harness=harness,
        native_id=uuid,
        date=d.get("date") or "",
        cwd=d.get("cwd") or "",
        last_active=d.get("last_active") or "",
        indexed_at=now,
        model=d.get("model"),
        repo=d.get("repo"),
        branch=d.get("branch"),
        forked_from=None,
        files=files,
        commits=list(d.get("commits") or []),
        prs=list(d.get("prs") or []),
        keywords=list(d.get("keywords") or []),
        skills=list(d.get("skills") or []),
        subagents=list(d.get("subagents") or []),
        mcp=list(d.get("mcp") or []),
        open_todos=list(d.get("open_todos") or []),
        tests=d.get("tests"),
        resolved=resolved,
        summary=summary,
        transcript_path=d.get("transcript_path") or "",
        excerpt_path=None,
        summary_state=state,
        parse_warnings=0,
    )


def _read_legacy(source: Path, res: ConvertResult) -> list[dict]:
    rows = []
    with open(source, "r", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                res.parse_errors += 1
    return rows


def convert(config, source_path=None, *, dry_run: bool = False) -> ConvertResult:
    """Migrate the legacy store into ``config``'s v1 store. Returns the counts.

    Holds the store-write lock around load→mutate→write (skipped for a dry run,
    which loads lock-free and writes nothing)."""
    source = Path(source_path) if source_path else DEFAULT_LEGACY_STORE
    if not source.exists():
        raise FileNotFoundError(str(source))

    res = ConvertResult()
    rows = _read_legacy(source, res)
    now = datetime.now(timezone.utc).astimezone().isoformat()
    store = Store(config)

    lock = None
    if not dry_run:
        lock = FileLock(str(config.store_lock_path), blocking=True, timeout=600).acquire()
    try:
        records = store.load()
        tombstones = store.load_tombstones()
        for d in rows:
            res.read += 1
            key = _legacy_key(d)
            if key is None:
                res.unknown_source += 1
                continue
            harness, uuid = key
            sid = make_sid(harness, uuid)

            if cwd_excluded(d.get("cwd") or "", config.exclusions):
                res.excluded += 1
                continue

            existing = records.get(sid)
            if existing is not None:
                # MERGE — graft only onto a not-yet-summarized record.
                if existing.summary_state not in _GRAFTABLE_STATES:
                    res.skipped += 1
                    continue
                parsed = parse_summary(d.get("summary"))
                if not any(parsed.values()):
                    res.skipped += 1  # nothing to graft
                    continue
                if not dry_run:
                    existing.summary = {k: strip_private(v) for k, v in parsed.items()}
                    existing.summary_state = "done"
                    old_res = d.get("resolved")
                    if old_res in RESOLVED_VALUES and old_res != "unknown":
                        existing.resolved = old_res
                    existing.keywords = _union(existing.keywords, d.get("keywords"))
                res.merged += 1
            else:
                # APPEND — no v1 twin; honor tombstones.
                if sid in tombstones:
                    res.tombstoned += 1
                    continue
                if not dry_run:
                    records[sid] = _to_record(d, harness, uuid, now, config.exclusions)
                res.appended += 1

        if not dry_run:
            store.write(records)
    finally:
        if lock is not None:
            lock.release()

    return res
