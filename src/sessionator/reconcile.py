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
   tombstoned sids are recorded in the watermark but not stored. A new session is
   ``pending``. A changed known session with no segments is marked ``stale`` (the
   whole record is re-summarized); one that already carries segments instead gets
   a debounced segment cut over just the turns that grew, and its state becomes
   the segment rollup — re-summarizing a long session from scratch on every
   keystroke is exactly what segments exist to avoid.
4. Kick the detached, self-terminating backfill (subprocess, never waited on).

``reconcile_one`` is the same pass narrowed to a single transcript: a hook says
"this session just compacted / ended", and one extraction plus one forced segment
cut follows. It shares Phase B with the full sweep via ``_commit``.

The store/watermark mutation runs under the store-write lock; extraction runs
outside it.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

from pathlib import Path

from . import segments as seg
from .adapters import ADAPTERS
from .locking import FileLock, LockBusy
from .privacy import cwd_excluded, scrub_files, scrub_text
from .schema import split_sid
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
    # sids upserted by this pass, in order — what a hook worker must summarize.
    touched_sids: list = None

    def __post_init__(self):
        if self.by_harness is None:
            self.by_harness = {}
        if self.touched_sids is None:
            self.touched_sids = []


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

    # Segment sidecars hold slices of the same text, so they need the same
    # redaction — an excluded path must appear in no derived file at all.
    for path in store.segment_sidecars(rec.sid):
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        new_text, n = scrub_text(text, globs)
        if n:
            try:
                path.write_text(new_text, encoding="utf-8")
            except OSError:
                continue
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

    records = _commit(config, store, extracted, new_watermarks, tombstones, result)
    _refresh_index(config, records)

    if kick_backfill:
        kick_detached_backfill(config)
    return result


def reconcile_one(
    config, path, *, sid: str | None = None, event: str | None = None,
    trigger: str | None = None,
) -> ReconcileResult:
    """Reconcile exactly one transcript and cut a segment at its current end.

    This is the hook path: something just happened to one session (a compaction,
    a session end), and re-scanning every transcript to find out would blow the
    hook's sub-second budget. ``event``/``trigger`` label the segment; the cut is
    forced, because a hook firing IS the user-visible moment worth summarizing.
    No backfill is kicked — the caller decides when to summarize which sid.
    """
    store = Store(config)
    result = ReconcileResult()
    p = Path(path)
    adapter = _adapter_for(p, sid)
    if adapter is None:
        return result

    result.scanned += 1
    try:
        st = p.stat()
    except OSError:
        return result
    try:
        rec = adapter.extract(p, config)
    except Exception:
        result.filtered += 1
        return result
    if rec is None:
        result.filtered += 1
        return result
    result.extracted += 1
    result.parse_warnings += rec.parse_warnings

    watermarks = store.load_watermarks()
    watermarks[str(p)] = [st.st_mtime, st.st_size]
    records = _commit(
        config, store, [rec], watermarks, store.load_tombstones(), result,
        event=event, trigger=trigger,
    )
    _refresh_index(config, records)
    return result


def _adapter_for(path: Path, sid: str | None):
    """The adapter that owns ``path``. The sid's harness prefix wins when the
    caller knows it; otherwise a Codex rollout is recognized by its filename."""
    if sid:
        try:
            harness = split_sid(sid)[0]
        except ValueError:
            harness = None
        if harness in ADAPTERS:
            return ADAPTERS[harness]
    if path.name.startswith("rollout-"):
        return ADAPTERS.get("codex")
    return ADAPTERS.get("claude")


def _commit(
    config, store, extracted, watermarks, tombstones, result, *,
    event: str | None = None, trigger: str | None = None,
):
    """Phase B, shared by the full sweep and the single-transcript hook path:
    under the store-write lock, purge excluded records, upsert the freshly
    extracted ones (cutting segments as needed), and persist store + watermarks.
    Returns the written records so the caller can refresh the index."""
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
                # and carry the segment trail over from the stored record.
                rec.summary = existing.summary
                rec.summary_segments = existing.summary_segments
                rec.summary_state = existing.summary_state
                rec.client = rec.client or existing.client
                result.upserted_changed += 1
            else:
                result.upserted_new += 1
            _cut_segments(store, rec, event=event, trigger=trigger)
            rec.excerpt_path = store.write_excerpt(rec)
            records[rec.sid] = rec
            result.touched_sids.append(rec.sid)
            result.by_harness[rec.harness] = result.by_harness.get(rec.harness, 0) + 1

        store.write(records)
        store.write_watermarks(watermarks)
        store.write_applied_exclusions(config.exclusions)
        return records
    finally:
        lock.release()


def _refresh_index(config, records) -> None:
    """Refresh the derived index from the just-written store. It is disposable
    and unlocked, so a failure here never fails the reconcile — a later query's
    ensure_current will rebuild it."""
    try:
        from .index import Index

        Index(config).ensure_current(records).close()
    except Exception:
        pass


def _cut_segments(store, rec, *, event, trigger) -> None:
    """Decide which summary segments this upsert opens, and set the record's
    summary_state accordingly.

    Three cases, in order:

    * the transcript carries its own cut points (Codex ``compacted`` lines) that
      no segment covers yet — replay them, then close the tail, so a session no
      hook ever saw still gets incremental summaries;
    * a hook told us what just happened — force one cut labelled with it;
    * the transcript merely grew — cut the new turns only, debounced, and only
      for a record that already has segments. A record with none stays on the
      legacy whole-record path (``stale``), which is also what keeps a plain
      ``ingest`` from opening any segment at all.
    """
    size = transcript_size(rec)
    turns = rec.turn_count or seg.count_turns(rec.excerpt)
    had_segments = bool(rec.summary_segments)
    cut = []

    replayed = seg.apply_boundaries(
        rec, rec.boundaries, turn_count=turns, size=size,
    )
    cut += replayed

    if event:
        forced = seg.append_segment(
            rec, event=event, trigger=trigger, turn_count=turns, size=size,
            force=True,
        )
        if forced:
            cut.append(forced)
    elif replayed:
        # Close the tail after the last replayed marker, or it would only ever be
        # summarized if the (possibly finished) session grew again.
        tail = seg.append_segment(
            rec, event="change", trigger=None, turn_count=turns, size=size,
            force=True,
        )
        if tail:
            cut.append(tail)
    elif had_segments and size > seg.last_bytes(rec.summary_segments):
        grown = seg.append_segment(
            rec, event="change", trigger=None, turn_count=turns, size=size,
        )
        if grown:
            cut.append(grown)

    for s in cut:
        _write_segment_sidecar(store, rec, s)

    if rec.summary_segments:
        rec.summary_state = seg.rollup_state(rec)
    elif rec.summary_state == "done":
        # Legacy whole-record path: the record changed, so its summary is stale.
        rec.summary_state = "stale"


def _write_segment_sidecar(store, rec, segment) -> None:
    """Persist the slice this segment covers while the untrimmed turns are still
    in hand — by summarizer time the stored excerpt may be middle-trimmed."""
    source = rec.excerpt_full or rec.excerpt
    text = seg.slice_turns(seg.split_turns(source), segment["start"], segment["end"])
    if text:
        store.write_segment_excerpt(rec, segment["seq"], text)


def transcript_size(rec) -> int:
    """The transcript's size right now — the growth marker a segment records."""
    if not rec.transcript_path:
        return 0
    try:
        return Path(rec.transcript_path).stat().st_size
    except OSError:
        return 0

    if kick_backfill:
        kick_detached_backfill(config)
    return result


def kick_detached_backfill(config, only_sid: str | None = None) -> None:
    """Spawn a detached, self-terminating backfill and return immediately. Any
    failure to spawn is swallowed — summaries are best-effort. ``only_sid``
    narrows the pass to one session (the hook path, which knows exactly which
    session just changed and must not pay for a store-wide sweep)."""
    try:
        logf = open(config.backfill_log_path, "a")
    except OSError:
        logf = subprocess.DEVNULL
    args = [sys.executable, "-m", "sessionator", "_backfill"]
    if only_sid:
        args.append("--sid")
        args.append(only_sid)
    try:
        subprocess.Popen(
            args,
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
