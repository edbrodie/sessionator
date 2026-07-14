"""On-disk store: records, excerpt sidecars, watermarks, tombstones.

Layout under the data dir:

* ``store.jsonl`` — one Record JSON object per line, keyed by ``sid``, rewritten
  atomically (temp + ``os.replace``), sorted (date desc, last_active desc).
* ``transcripts/<harness>-<uuid>.md`` — the capped, private-stripped excerpt
  sidecar, pruning-proof (``show`` falls back to it).
* ``watermarks.json`` — ``{transcript_path: [mtime, size]}`` for the reconcile
  scan.
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

from .schema import Record, split_sid


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

    def read_excerpt(self, rec: Record) -> str:
        """Return the excerpt body (without the sidecar header), or ''."""
        p = Path(rec.excerpt_path) if rec.excerpt_path else self.excerpt_path_for(rec.sid)
        if not p.exists():
            return ""
        text = p.read_text(errors="replace")
        _, sep, body = text.partition("\n---\n\n")
        return body if sep else text

    def delete_excerpt(self, sid: str) -> None:
        try:
            self.excerpt_path_for(sid).unlink()
        except (OSError, ValueError):
            pass

    # --- watermarks ------------------------------------------------------
    def load_watermarks(self) -> dict:
        return _load_json(self.watermarks_path, default={})

    def write_watermarks(self, wm: dict) -> None:
        _dump_json(self.watermarks_path, wm)

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
