"""On-disk store: records, excerpt sidecars, watermarks, tombstones.

Layout under the data dir:

* ``store.jsonl`` — one Record JSON object per line, keyed by ``sid``, rewritten
  atomically (temp + ``os.replace``), sorted (date desc, last_active desc).
* ``transcripts/<harness>-<uuid>.md`` — the capped, private-stripped excerpt
  sidecar, pruning-proof (``show`` falls back to it).
* ``transcripts/<harness>-<uuid>.seg<seq>.md`` — one per summary segment: the
  slice of the excerpt that cut covered, written at cut time. It exists because
  the main excerpt is middle-trimmed at 36k, and the sessions that compact are
  exactly the long ones whose middle would be gone by the time the summarizer
  ran; the sidecar preserves the slice verbatim (capped for the LLM).
* ``watermarks.json`` — ``{"schema": 2, "entries": {key: [mtime, size]}}`` for
  the reconcile scan. The key is the adapter's stable identity for a transcript
  (``codex:<uuid>``, ``claude:<stem>``), not its path: Codex archives a thread by
  **moving** its rollout, and a path-keyed watermark reads that move as a new
  transcript and re-extracts the whole session. Schema-1 files (path-keyed, no
  wrapper) are migrated on load.
* ``tombstones.json`` — a list of sids that ``forget`` has retired; reconcile
  never re-ingests them.
* ``scrub_state.json`` — the exclusion globs last applied by a retroactive
  files/excerpt scrub; a change (or absence) triggers the sweep (T-012).

Callers hold the store-write lock (``locking.FileLock``) around the load →
mutate → write critical section. Reads are lock-free.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .adapters import adapter_for_path
from .schema import Record, split_sid
from .segments import INPUT_CAP, cap_text

WATERMARK_SCHEMA = 2


class Store:
    def __init__(self, config):
        self.config = config
        self.data_dir = Path(config.data_dir)
        self.store_path = self.data_dir / "store.jsonl"
        self.transcripts_dir = self.data_dir / "transcripts"
        self.watermarks_path = self.data_dir / "watermarks.json"
        self.tombstones_path = self.data_dir / "tombstones.json"
        self.scrub_state_path = self.data_dir / "scrub_state.json"

    # --- records ---------------------------------------------------------
    def load(self) -> dict[str, Record]:
        out = {}
        if not self.store_path.exists():
            return out
        with open(self.store_path, "r", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                sid = d.get("sid")
                if sid:
                    out[sid] = Record.from_dict(d)
        return out

    def write(self, records: dict[str, Record]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        ordered = sorted(
            records.values(),
            key=lambda r: (r.date or "", r.last_active or ""),
            reverse=True,
        )
        tmp = self.store_path.with_suffix(".jsonl.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for rec in ordered:
                f.write(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n")
        os.replace(tmp, self.store_path)

    # --- excerpt sidecars ------------------------------------------------
    def excerpt_path_for(self, sid: str) -> Path:
        harness, uuid = split_sid(sid)
        return self.transcripts_dir / f"{harness}-{uuid}.md"

    def write_excerpt(self, rec: Record) -> str | None:
        """Persist the record's transient excerpt to its sidecar; returns the
        sidecar path (str) or None when there is no excerpt."""
        if not rec.excerpt:
            return None
        self.transcripts_dir.mkdir(parents=True, exist_ok=True)
        path = self.excerpt_path_for(rec.sid)
        tmp = path.with_suffix(".md.tmp")
        header = f"# {rec.sid}\n\n_{rec.harness} · {rec.date} · {rec.cwd}_\n\n---\n\n"
        tmp.write_text(header + rec.excerpt + "\n", encoding="utf-8")
        os.replace(tmp, path)
        return str(path)

    # --- per-segment excerpt sidecars ------------------------------------
    def segment_excerpt_path_for(self, sid: str, seq: int) -> Path:
        harness, uuid = split_sid(sid)
        return self.transcripts_dir / f"{harness}-{uuid}.seg{int(seq)}.md"

    def write_segment_excerpt(self, rec: Record, seq: int, text: str) -> str | None:
        """Persist one segment's slice of the excerpt, capped to the summarizer
        input budget. Returns the path, or None when the slice is empty."""
        if not text or not text.strip():
            return None
        self.transcripts_dir.mkdir(parents=True, exist_ok=True)
        path = self.segment_excerpt_path_for(rec.sid, seq)
        tmp = path.with_suffix(".md.tmp")
        header = f"# {rec.sid} seg{int(seq)}\n\n_{rec.harness} · {rec.date}_\n\n---\n\n"
        tmp.write_text(header + cap_text(text, INPUT_CAP) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        return str(path)

    def read_segment_excerpt(self, rec: Record, seq: int) -> str:
        """The segment slice written at cut time, or '' when absent (the caller
        then re-slices the main excerpt)."""
        return _read_sidecar(self.segment_excerpt_path_for(rec.sid, seq))

    def segment_sidecars(self, sid: str):
        """Every segment sidecar of ``sid`` (used by delete and by the scrub)."""
        try:
            harness, uuid = split_sid(sid)
        except ValueError:
            return []
        return sorted(self.transcripts_dir.glob(f"{harness}-{uuid}.seg*.md"))

    def read_excerpt(self, rec: Record) -> str:
        """Return the excerpt body (without the sidecar header), or ''."""
        p = Path(rec.excerpt_path) if rec.excerpt_path else self.excerpt_path_for(rec.sid)
        return _read_sidecar(p)

    def delete_excerpt(self, sid: str) -> None:
        """Drop the excerpt sidecar and every segment sidecar of ``sid`` — a
        forgotten session must leave no derived text behind."""
        for p in [self.excerpt_path_for(sid), *self.segment_sidecars(sid)]:
            try:
                p.unlink()
            except (OSError, ValueError):
                pass

    # --- watermarks ------------------------------------------------------
    def load_watermarks(self) -> dict:
        """The ``{key: [mtime, size]}`` entries, migrating a v1 file in passing.
        Returns the entries alone — the schema wrapper is this module's business,
        not the reconcile's."""
        data = _load_json(self.watermarks_path, default={})
        if not isinstance(data, dict):
            return {}
        if data.get("schema") == WATERMARK_SCHEMA:
            entries = data.get("entries")
            return entries if isinstance(entries, dict) else {}
        return self._migrate_watermarks(data)

    def _migrate_watermarks(self, v1: dict) -> dict:
        """v1 keyed every entry on the transcript path. Re-key each one through
        the owning adapter so the history survives; a path no adapter recognizes
        keeps a ``path:`` key, which is exactly what it meant before. Nothing is
        written here — the next reconcile persists the v2 file."""
        out = {}
        for key, value in v1.items():
            if not isinstance(key, str):
                continue
            adapter = adapter_for_path(self.config, key)
            out[watermark_key(adapter, key)] = value
        return out

    def write_watermarks(self, wm: dict) -> None:
        _dump_json(
            self.watermarks_path, {"schema": WATERMARK_SCHEMA, "entries": wm}
        )

    # --- tombstones ------------------------------------------------------
    def load_tombstones(self) -> set[str]:
        data = _load_json(self.tombstones_path, default=[])
        return set(data) if isinstance(data, list) else set()

    def write_tombstones(self, sids) -> None:
        _dump_json(self.tombstones_path, sorted(set(sids)))

    def is_tombstoned(self, sid: str, tombstones: set[str]) -> bool:
        return sid in tombstones

    # --- scrub state -----------------------------------------------------
    def load_applied_exclusions(self):
        """The exclusion globs a retroactive scrub last ran with, or None when
        the sweep has never run (a new store, or the scrub feature just landed).
        None forces a first sweep so pre-existing records are cleaned."""
        data = _load_json(self.scrub_state_path, default=None)
        if isinstance(data, dict) and isinstance(data.get("exclusions"), list):
            return data["exclusions"]
        return None

    def write_applied_exclusions(self, globs) -> None:
        _dump_json(self.scrub_state_path, {"exclusions": sorted(set(globs or []))})


def watermark_key(adapter, path) -> str:
    """The watermark key for ``path`` under ``adapter``.

    ``watermark_key`` is the optional fifth adapter function: an adapter that
    knows a path-independent identity for its transcripts declares it, and gets
    move-tolerance for free. One that does not falls back to ``path:<path>``,
    which is the v1 behaviour.
    """
    fn = getattr(adapter, "watermark_key", None)
    if callable(fn):
        try:
            key = fn(path)
        except Exception:
            key = None
        if isinstance(key, str) and key:
            return key
    return f"path:{path}"


def _read_sidecar(path: Path) -> str:
    """A sidecar's body, without the ``# sid … ---`` header, or ''."""
    if not path.exists():
        return ""
    text = path.read_text(errors="replace")
    _, sep, body = text.partition("\n---\n\n")
    return body if sep else text


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with open(path, "r", errors="replace") as f:
            return json.load(f)
    except Exception:
        return default


def _dump_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=0)
    os.replace(tmp, path)
