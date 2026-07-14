"""Derived SQLite/FTS5 index (T-008 resolution).

The index is a *disposable* projection of ``store.jsonl`` — the JSONL is the sole
source of truth and the only source of query *output*; this file only ranks. It
is rebuilt from scratch on schema mismatch or corruption and never migrated.

Layout (``$XDG_DATA_HOME/sessionator/index.db``, ``PRAGMA user_version = 1``):

* ``sessions`` — one row per record, the filterable scalar columns.
* ``fts`` — FTS5 ``porter unicode61 tokenchars '-_./'`` over the twelve text
  columns (keeps ``snake_case``/paths/versions whole while stemming prose).
* ``fts_tri`` — FTS5 ``trigram`` over one concatenated haystack, for
  fuzzy/substring. Feature-detected (needs SQLite ≥3.34); absent → skipped.
* ``watermarks`` — ``(path, mtime, size)`` gate over ``store.jsonl`` so an
  unchanged store is a no-op.

When FTS5 is entirely unavailable (an exotic Python SQLite build) the index is
inert and :mod:`sessionator.search` falls back to a linear scan behind the same
API.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .schema import Record

USER_VERSION = 1

# Order matters: it is the column order for both the DDL and every INSERT.
FTS_COLUMNS = (
    "asked",
    "learned",
    "completed",
    "left_off",
    "next_steps",
    "keywords",
    "cwd",
    "repo",
    "branch",
    "commit_subjects",
    "file_paths",
    "skills",
)

_PORTER_TOKENIZE = "porter unicode61 tokenchars '-_./'"


# ---------------------------------------------------------------------------
# Row projection: Record -> index column values. Also used by the linear-scan
# fallback so the two search paths share one definition of the haystack.
# ---------------------------------------------------------------------------

def fts_values(rec: Record) -> tuple[str, ...]:
    """The twelve ``fts`` column strings for a record, in :data:`FTS_COLUMNS`
    order."""
    s = rec.summary or {}
    return (
        s.get("asked", "") or "",
        s.get("learned", "") or "",
        s.get("completed", "") or "",
        s.get("left_off", "") or "",
        s.get("next_steps", "") or "",
        " ".join(str(k) for k in (rec.keywords or [])),
        rec.cwd or "",
        rec.repo or "",
        rec.branch or "",
        " ".join(str(subj) for _sha, subj in (rec.commits or [])),
        " ".join(str(p) for _op, p in (rec.files or [])),
        " ".join(str(s) for s in (rec.skills or [])),
    )


def haystack(rec: Record) -> str:
    """The full lowercased free-text corpus for a record — a superset of the
    ``fts`` columns (adds subagent types, mcp, open todos, model, native_id) used
    for the trigram content column and the linear-scan matcher, so a bare-term
    query hits the same surface whichever path runs."""
    parts = list(fts_values(rec))
    parts += [str(t) for t, _c in (rec.subagents or [])]
    parts += [str(m) for m in (rec.mcp or [])]
    parts += [str(t) for t in (rec.open_todos or [])]
    if rec.model:
        parts.append(rec.model)
    parts.append(rec.native_id or "")
    return "\n".join(p for p in parts if p).lower()


def _has_summary(rec: Record) -> int:
    return 1 if any((rec.summary or {}).values()) else 0


# ---------------------------------------------------------------------------
# Index handle.
# ---------------------------------------------------------------------------

class Index:
    """Open/rebuild/sync the index and expose the two FTS rank lists.

    A single instance is cheap; ``ensure_current`` is idempotent and gated by the
    store watermark so repeated calls in one process are near-free.
    """

    def __init__(self, config):
        self.config = config
        self.path = Path(config.index_path)
        self.store_path = Path(config.store_path)
        self._conn: sqlite3.Connection | None = None
        self.has_fts = False
        self.has_tri = False

    # --- connection / schema -------------------------------------------------
    def connect(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path))
        conn.execute("PRAGMA journal_mode=WAL")
        self.has_fts, self.has_tri = _probe_features(conn)
        if not self.has_fts:
            # No FTS5 at all: the index cannot help. Leave it untouched; search
            # will run the linear fallback.
            self._conn = conn
            return conn
        if not _schema_ok(conn, self.has_tri):
            _rebuild_schema(conn, self.has_tri)
        self._conn = conn
        return conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # --- incremental sync ----------------------------------------------------
    def ensure_current(self, records: dict[str, Record]) -> "Index":
        """Bring the index in line with ``records`` (the loaded store). Fast path:
        an unchanged ``store.jsonl`` (matching watermark) returns immediately.
        Returns ``self`` so ``.has_fts`` is available to the caller."""
        conn = self.connect()
        if not self.has_fts:
            return self
        gate = _store_stat(self.store_path)
        if gate is not None and _watermark_matches(conn, str(self.store_path), gate):
            return self
        self._sync(conn, records)
        if gate is not None:
            _write_watermark(conn, str(self.store_path), gate)
        conn.commit()
        return self

    def _sync(self, conn: sqlite3.Connection, records: dict[str, Record]) -> None:
        existing = {
            sid: indexed_at
            for sid, indexed_at in conn.execute(
                "SELECT sid, indexed_at FROM sessions"
            ).fetchall()
        }
        want = set(records)
        for sid in set(existing) - want:
            _delete_sid(conn, sid, self.has_tri)
        for sid, rec in records.items():
            if sid in existing and existing[sid] == (rec.indexed_at or ""):
                continue
            if sid in existing:
                _delete_sid(conn, sid, self.has_tri)
            _insert(conn, rec, self.has_tri)

    def delete_sids(self, sids) -> None:
        """Drop the given sids from every index table. Used by ``forget`` to
        purge index rows immediately, without waiting for the next sync. A no-op
        when FTS5 is unavailable (the index is inert)."""
        conn = self.connect()
        if not self.has_fts:
            return
        for sid in sids:
            _delete_sid(conn, sid, self.has_tri)
        conn.commit()

    # --- ranking -------------------------------------------------------------
    def fts_rank_lists(self, terms: list[str]) -> tuple[list[str], list[str]]:
        """Two best-first sid lists — porter ``fts`` and trigram ``fts_tri`` —
        for the AND-matched ``terms``. Either may be empty (no hits, syntax error,
        or table absent)."""
        conn = self.connect()
        porter = _match_sids(conn, "fts", _porter_query(terms))
        tri = []
        if self.has_tri:
            tq = _trigram_query(terms)
            if tq:
                tri = _match_sids(conn, "fts_tri", tq)
        return porter, tri


# ---------------------------------------------------------------------------
# Feature detection.
# ---------------------------------------------------------------------------

def _probe_features(conn: sqlite3.Connection) -> tuple[bool, bool]:
    has_fts = _can_create(conn, "porter unicode61")
    has_tri = has_fts and _can_create(conn, "trigram")
    return has_fts, has_tri


def _can_create(conn: sqlite3.Connection, tokenize: str) -> bool:
    try:
        conn.execute(
            f"CREATE VIRTUAL TABLE temp.__probe USING fts5(x, tokenize='{tokenize}')"
        )
        conn.execute("DROP TABLE temp.__probe")
        return True
    except sqlite3.Error:
        return False


# ---------------------------------------------------------------------------
# Schema build / check / teardown.
# ---------------------------------------------------------------------------

def _schema_ok(conn: sqlite3.Connection, has_tri: bool) -> bool:
    if conn.execute("PRAGMA user_version").fetchone()[0] != USER_VERSION:
        return False
    names = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        ).fetchall()
    }
    required = {"sessions", "fts", "watermarks"}
    if has_tri:
        required.add("fts_tri")
    return required <= names


def _rebuild_schema(conn: sqlite3.Connection, has_tri: bool) -> None:
    for tbl in ("fts", "fts_tri", "sessions", "watermarks"):
        conn.execute(f"DROP TABLE IF EXISTS {tbl}")
    conn.execute(
        """
        CREATE TABLE sessions (
            rowid       INTEGER PRIMARY KEY,
            sid         TEXT UNIQUE NOT NULL,
            harness     TEXT,
            model       TEXT,
            date        TEXT,
            last_active TEXT,
            cwd         TEXT,
            repo        TEXT,
            branch      TEXT,
            resolved    TEXT,
            has_summary INTEGER,
            indexed_at  TEXT
        )
        """
    )
    cols = ", ".join(FTS_COLUMNS)
    conn.execute(
        f"CREATE VIRTUAL TABLE fts USING fts5({cols}, tokenize=\"{_PORTER_TOKENIZE}\")"
    )
    if has_tri:
        conn.execute(
            "CREATE VIRTUAL TABLE fts_tri USING fts5(haystack, tokenize='trigram')"
        )
    conn.execute(
        "CREATE TABLE watermarks (path TEXT PRIMARY KEY, mtime REAL, size INTEGER)"
    )
    conn.execute(f"PRAGMA user_version = {USER_VERSION}")
    conn.commit()


# ---------------------------------------------------------------------------
# Row mutation.
# ---------------------------------------------------------------------------

def _insert(conn: sqlite3.Connection, rec: Record, has_tri: bool) -> None:
    cur = conn.execute(
        """
        INSERT INTO sessions
            (sid, harness, model, date, last_active, cwd, repo, branch,
             resolved, has_summary, indexed_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            rec.sid,
            rec.harness,
            rec.model,
            rec.date,
            rec.last_active,
            rec.cwd,
            rec.repo,
            rec.branch,
            rec.resolved,
            _has_summary(rec),
            rec.indexed_at or "",
        ),
    )
    rowid = cur.lastrowid
    placeholders = ",".join(["?"] * (len(FTS_COLUMNS) + 1))
    conn.execute(
        f"INSERT INTO fts(rowid, {', '.join(FTS_COLUMNS)}) VALUES ({placeholders})",
        (rowid, *fts_values(rec)),
    )
    if has_tri:
        conn.execute(
            "INSERT INTO fts_tri(rowid, haystack) VALUES (?, ?)",
            (rowid, haystack(rec)),
        )


def _delete_sid(conn: sqlite3.Connection, sid: str, has_tri: bool) -> None:
    row = conn.execute("SELECT rowid FROM sessions WHERE sid=?", (sid,)).fetchone()
    if not row:
        return
    rowid = row[0]
    conn.execute("DELETE FROM fts WHERE rowid=?", (rowid,))
    if has_tri:
        conn.execute("DELETE FROM fts_tri WHERE rowid=?", (rowid,))
    conn.execute("DELETE FROM sessions WHERE rowid=?", (rowid,))


# ---------------------------------------------------------------------------
# Watermark helpers.
# ---------------------------------------------------------------------------

def _store_stat(path: Path) -> tuple[float, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime, st.st_size)


def _watermark_matches(conn, path: str, gate: tuple[float, int]) -> bool:
    row = conn.execute(
        "SELECT mtime, size FROM watermarks WHERE path=?", (path,)
    ).fetchone()
    return bool(row) and row[0] == gate[0] and row[1] == gate[1]


def _write_watermark(conn, path: str, gate: tuple[float, int]) -> None:
    conn.execute(
        "INSERT INTO watermarks(path, mtime, size) VALUES (?,?,?) "
        "ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime, size=excluded.size",
        (path, gate[0], gate[1]),
    )


# ---------------------------------------------------------------------------
# MATCH query construction + execution.
# ---------------------------------------------------------------------------

def _porter_query(terms: list[str]) -> str:
    """Quote each term as an FTS5 phrase and AND them (whitespace = implicit
    AND). Quoting neutralises FTS5 operators in user input."""
    return " ".join('"' + t.replace('"', '""') + '"' for t in terms if t)


def _trigram_query(terms: list[str]) -> str:
    """Trigram matching needs ≥3-char tokens; shorter terms are dropped. Returns
    '' when nothing qualifies (caller then skips the trigram list)."""
    usable = [t for t in terms if len(t) >= 3]
    return _porter_query(usable)


def _match_sids(conn: sqlite3.Connection, table: str, query: str) -> list[str]:
    if not query:
        return []
    try:
        # FTS5's MATCH/bm25 require the table name literally (an alias is
        # rejected with "no such column"), so join by rowid without aliasing it.
        rows = conn.execute(
            f"SELECT s.sid FROM {table} JOIN sessions s ON s.rowid = {table}.rowid "
            f"WHERE {table} MATCH ? ORDER BY bm25({table})",
            (query,),
        ).fetchall()
    except sqlite3.Error:
        # Malformed MATCH (defensive) → no hits from this table.
        return []
    return [r[0] for r in rows]
