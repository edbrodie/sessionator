"""Reconcile: deterministic ingest, then kick the detached backfill.

The inline pass is fast and never calls an LLM:

1. Retroactive exclusion purge — drop any stored record whose cwd now matches an
   exclusion glob (adding an exclusion removes data, not just future data; T-003).
   Kept records are also scrubbed: any ``files`` entry or excerpt path string
   matching an exclusion glob is redacted, so an excluded path appears nowhere in
   the store (T-012). This sidecar-touching sweep runs only when the glob set
   changed since it last ran (tracked in ``scrub_state.json``) so the steady-state
   reconcile stays I/O-free; freshly extracted records are always scrubbed inline.
2. Watermark scan — for each adapter, enumerate candidate transcripts; anything
   whose (mtime, size) is unchanged since last seen is skipped. New/changed files
   are extracted deterministically.
3. Upsert — filtered sessions (extract returns None), excluded cwds, and
   tombstoned sids are recorded in the watermark but not stored. A changed known
   session is rebuilt and its summary marked ``stale``; a new one is ``pending``.
4. Kick the detached, self-terminating backfill (subprocess, never waited on).

The store/watermark mutation runs under the store-write lock; extraction runs
outside it.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

from .adapters import ADAPTERS
from .locking import FileLock, LockBusy
from .privacy import cwd_excluded, scrub_files, scrub_text
from .store import Store


@dataclass
class ReconcileResult:
    scanned: int = 0
    extracted: int = 0
    upserted_new: int = 0
    upserted_changed: int = 0
    filtered: int = 0
    excluded: int = 0
    tombstoned_skipped: int = 0
    purged: int = 0
    scrubbed: int = 0
    by_harness: dict = None
    parse_warnings: int = 0

    def __post_init__(self):
        if self.by_harness is None:
            self.by_harness = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _scrub_stored_record(store, rec, globs) -> bool:
    """Redact excluded paths from an already-stored record in place: its ``files``
    manifest (mutated on ``rec``) and its excerpt sidecar (rewritten on disk).
    Returns True if anything was redacted. Idempotent — a clean record is a
    no-op and its sidecar is not rewritten."""
    changed = False

    rec.files, n_files = scrub_files(rec.files, globs)
    if n_files:
        changed = True

    body = store.read_excerpt(rec)
    if body:
        new_body, n_excerpt = scrub_text(body, globs)
        if n_excerpt:
            rec.excerpt = new_body
            rec.excerpt_path = store.write_excerpt(rec)
            changed = True
    return changed


def reconcile(config, *, kick_backfill: bool = True) -> ReconcileResult:
    store = Store(config)
    result = ReconcileResult()
    tombstones = store.load_tombstones()

    # Phase A (no lock): extract everything new/changed.
    watermarks = store.load_watermarks()
    new_watermarks = dict(watermarks)
    extracted = []  # list[Record]
    for name, adapter in ADAPTERS.items():
        for root in adapter.discover_sources(config):
            for path, mtime, size in adapter.enumerate_sessions(root):
                result.scanned += 1
                key = str(path)
                prev = watermarks.get(key)
                if prev == [mtime, size]:
                    continue  # unchanged since last seen
                new_watermarks[key] = [mtime, size]
                try:
                    rec = adapter.extract(path, config)
                except Exception:
                    # A single bad file must never break the sweep (T-007 rule 5).
                    result.filtered += 1
                    continue
                if rec is None:
                    result.filtered += 1
                    continue
                result.extracted += 1
                result.parse_warnings += rec.parse_warnings
                extracted.append(rec)

    # Phase B (under lock): purge + upsert + persist.
    try:
        lock = FileLock(str(config.store_lock_path), blocking=True, timeout=600).acquire()
    except LockBusy:
        # Someone else is mid-write; try again shortly by failing loudly upstream.
        raise
    try:
        records = store.load()

        # 1. Retroactive exclusion purge + path scrub of the kept records.
        scrub_globs_changed = store.load_applied_exclusions() != sorted(
            set(config.exclusions or [])
        )
        for sid in list(records):
            rec = records[sid]
            if cwd_excluded(rec.cwd, config.exclusions):
                del records[sid]
                store.delete_excerpt(sid)
                result.purged += 1
                continue
            # A kept record may still carry an excluded path in its files
            # manifest or excerpt (e.g. a non-vault session that wrote a vault
            # file). Re-scrub only when the glob set changed since last time — a
            # sidecar-reading sweep is too costly for every steady-state run, and
            # nothing new appears in a pre-existing record between glob changes.
            if scrub_globs_changed and _scrub_stored_record(
                store, rec, config.exclusions
            ):
                result.scrubbed += 1

        # 2/3. Upsert the freshly extracted records.
        for rec in extracted:
            if cwd_excluded(rec.cwd, config.exclusions):
                # Never ingest excluded sessions; drop any pre-existing copy too.
                if rec.sid in records:
                    del records[rec.sid]
                    store.delete_excerpt(rec.sid)
                result.excluded += 1
                continue
            if store.is_tombstoned(rec.sid, tombstones):
                result.tombstoned_skipped += 1
                continue

            # Scrub excluded paths from a freshly extracted record before it is
            # ever written (ingest-side half of T-012).
            rec.files, n_files = scrub_files(rec.files, config.exclusions)
            rec.excerpt, n_excerpt = scrub_text(rec.excerpt, config.exclusions)
            if n_files or n_excerpt:
                result.scrubbed += 1

            existing = records.get(rec.sid)
            rec.indexed_at = _now_iso()
            if existing is not None:
                # Rebuild-on-change: keep the (possibly outdated) summary visible
                # but mark it stale so the backfill re-summarizes it.
                rec.summary = existing.summary
                rec.summary_state = "stale"
                result.upserted_changed += 1
            else:
                result.upserted_new += 1
            rec.excerpt_path = store.write_excerpt(rec)
            records[rec.sid] = rec
            result.by_harness[rec.harness] = result.by_harness.get(rec.harness, 0) + 1

        store.write(records)
        store.write_watermarks(new_watermarks)
        store.write_applied_exclusions(config.exclusions)
    finally:
        lock.release()

    # Refresh the derived index from the just-written store. It is disposable and
    # unlocked, so a failure here never fails the reconcile — a later query's
    # ensure_current will rebuild it.
    try:
        from .index import Index

        Index(config).ensure_current(records).close()
    except Exception:
        pass

    if kick_backfill:
        kick_detached_backfill(config)
    return result


def kick_detached_backfill(config) -> None:
    """Spawn a detached, self-terminating backfill and return immediately. Any
    failure to spawn is swallowed — summaries are best-effort."""
    try:
        logf = open(config.backfill_log_path, "a")
    except OSError:
        logf = subprocess.DEVNULL
    try:
        subprocess.Popen(
            [sys.executable, "-m", "sessionator", "_backfill"],
            stdin=subprocess.DEVNULL,
            stdout=logf,
            stderr=logf,
            start_new_session=True,
            close_fds=True,
        )
    except Exception:
        pass
    finally:
        if logf not in (subprocess.DEVNULL, None):
            try:
                logf.close()
            except OSError:
                pass
