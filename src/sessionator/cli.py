"""Command-line entry point for sessionator.

Six product verbs (T-002), search implicit:

* ``sessionator <terms…> [filters]`` — bare invocation IS search (the 90% case).
* ``sessionator show <sid-prefix>`` — full record + transcript tail + resume line.
* ``sessionator resume <sid-prefix>`` — exactly one line: the resume invocation.
* ``sessionator ingest`` — manual reconcile.
* ``sessionator status`` — config/sources/index health (doubles as doctor).
* ``sessionator forget <sid|pattern>`` — privacy retire (stub here).

Every command except the hidden ``_backfill`` runs a fast, non-blocking reconcile
first (deterministic inline pass + detached summary backfill), so a query always
sees the just-finished session and never waits on summaries.

Output discipline: **stdout = data only, stderr = diagnostics.** Exit codes
follow grep — ``0`` hits, ``1`` zero hits, ``2`` error — so scripts branch
without parsing text.
"""

from __future__ import annotations

import argparse
import sys

from . import __version__

# Commands that take an explicit verb. Anything else is implicit search.
# ``convert-legacy`` is a hidden one-time migration verb (absent from help).
_COMMANDS = (
    "search", "show", "resume", "ingest", "status", "forget",
    "_backfill", "convert-legacy",
)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if argv and argv[0] == "--version":
        print(f"sessionator {__version__}")
        return 0
    if not argv:
        return _cmd_search([])  # bare `sessionator` → recent sessions
    if argv[0] in ("-h", "--help"):
        _print_top_help()
        return 0

    if argv[0] in _COMMANDS:
        command, rest = argv[0], argv[1:]
    else:
        command, rest = "search", argv  # implicit search

    if command == "search":
        return _cmd_search(rest)
    if command == "show":
        return _cmd_show(rest)
    if command == "resume":
        return _cmd_resume(rest)
    if command == "ingest":
        return _cmd_ingest(rest)
    if command == "status":
        return _cmd_status(rest)
    if command == "_backfill":
        return _cmd_backfill(rest)
    if command == "forget":
        return _cmd_forget(rest)
    if command == "convert-legacy":
        return _cmd_convert(rest)

    _print_top_help()
    return 0


def _print_top_help() -> None:
    print(
        "usage: sessionator <terms…> [filters]      search (implicit)\n"
        "       sessionator show <sid-prefix>       full record + transcript tail\n"
        "       sessionator resume <sid-prefix>     print the resume invocation\n"
        "       sessionator ingest                  scan transcripts, update index\n"
        "       sessionator status                  config, sources, index health\n"
        "       sessionator forget <sid|cwd-glob>   retire session(s): delete +\n"
        "                                           tombstone so ingest won't re-add\n"
        "\nfilters: --keyword --repo --cwd --model --harness --since --until "
        "--resolved --limit --format {compact,full,ndjson}\n"
        "privacy: config [exclusions].cwd_globs removes matching sessions (retro-\n"
        "         purged) and scrubs matching paths everywhere; forget --dry-run\n"
        "         previews; see `sessionator status` for the tombstone count."
    )


# ---------------------------------------------------------------------------
# Config load + fast reconcile (shared preamble).
# ---------------------------------------------------------------------------

def _load_config_verbose():
    from .config import load

    cfg = load()
    if cfg.first_run:
        print(f"First run — wrote config to {cfg.path}", file=sys.stderr)
        for note in cfg.detection_notes:
            print(f"  {note}", file=sys.stderr)
    return cfg


def _fast_reconcile(cfg) -> None:
    """Deterministic inline reconcile + detached backfill kick. Never blocks a
    query and never fails one: any error is a diagnostic on stderr."""
    try:
        from .reconcile import reconcile

        reconcile(cfg, kick_backfill=True)
    except Exception as e:  # pragma: no cover - defensive
        print(f"reconcile skipped: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# search (implicit)
# ---------------------------------------------------------------------------

def _build_search_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="sessionator", description="Search the session history."
    )
    ap.add_argument("terms", nargs="*", help="free-text terms (AND, case-insensitive)")
    ap.add_argument("--keyword", action="append", default=[], help="exact keyword (repeatable)")
    ap.add_argument("--repo", help="repo substring")
    ap.add_argument("--cwd", help="cwd substring")
    ap.add_argument("--model", help="model substring")
    ap.add_argument("--harness", choices=["claude", "codex"])
    ap.add_argument("--since", help="inclusive lower date bound YYYY-MM-DD")
    ap.add_argument("--until", help="inclusive upper date bound YYYY-MM-DD")
    ap.add_argument("--resolved", choices=["open", "done", "unknown"])
    ap.add_argument("--limit", type=int, default=20, help="max results (0 = all)")
    ap.add_argument(
        "--format", choices=["compact", "full", "ndjson"], default="compact"
    )
    ap.add_argument("--no-reconcile", action="store_true", help=argparse.SUPPRESS)
    return ap


def _cmd_search(argv: list[str]) -> int:
    ap = _build_search_parser()
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return int(e.code) if e.code else 0

    from .render import render_compact, render_full, render_ndjson
    from .search import Filters, search
    from .store import Store

    try:
        cfg = _load_config_verbose()
        if not args.no_reconcile:
            _fast_reconcile(cfg)
        records = Store(cfg).load()
        filters = Filters(
            keyword=args.keyword,
            repo=args.repo,
            cwd=args.cwd,
            model=args.model,
            harness=args.harness,
            since=args.since,
            until=args.until,
            resolved=args.resolved,
        )
        hits = search(cfg, args.terms, filters, limit=args.limit, records=records)
    except Exception as e:  # pragma: no cover - defensive top-level
        print(f"search error: {e}", file=sys.stderr)
        return 2

    renderers = {
        "compact": render_compact,
        "full": render_full,
        "ndjson": render_ndjson,
    }
    out = renderers[args.format](hits)
    if out:
        sys.stdout.write(out + "\n")
    print(_plural(len(hits)), file=sys.stderr)
    return 0 if hits else 1


def _plural(n: int) -> str:
    return f"{n} match" if n == 1 else f"{n} matches"


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------

def _cmd_show(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="sessionator show")
    ap.add_argument("sid_prefix", help="unique sid prefix (e.g. claude/d78b1004)")
    ap.add_argument("--tail", type=int, default=4000, help="chars of transcript (default 4000)")
    ap.add_argument("--no-reconcile", action="store_true", help=argparse.SUPPRESS)
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return int(e.code) if e.code else 0

    from .show import render_show
    from .store import Store

    try:
        cfg = _load_config_verbose()
        if not args.no_reconcile:
            _fast_reconcile(cfg)
        store = Store(cfg)
        records = store.load()
    except Exception as e:  # pragma: no cover - defensive top-level
        print(f"show error: {e}", file=sys.stderr)
        return 2

    rec = _resolve_one(records, args.sid_prefix)
    if rec is None:
        return 2
    sys.stdout.write(render_show(store, rec, args.tail) + "\n")
    return 0


# ---------------------------------------------------------------------------
# resume
# ---------------------------------------------------------------------------

def _cmd_resume(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="sessionator resume")
    ap.add_argument("sid_prefix", help="unique sid prefix")
    ap.add_argument("--no-reconcile", action="store_true", help=argparse.SUPPRESS)
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return int(e.code) if e.code else 0

    from .resume import resume_string
    from .store import Store

    try:
        cfg = _load_config_verbose()
        if not args.no_reconcile:
            _fast_reconcile(cfg)
        records = Store(cfg).load()
    except Exception as e:  # pragma: no cover - defensive top-level
        print(f"resume error: {e}", file=sys.stderr)
        return 2

    rec = _resolve_one(records, args.sid_prefix)
    if rec is None:
        return 2
    sys.stdout.write(resume_string(rec) + "\n")
    return 0


def _resolve_one(records, prefix):
    """Resolve a sid-prefix to exactly one record, or print an error to stderr
    and return None. Ambiguity lists candidates."""
    from .lookup import find_by_prefix

    matches = find_by_prefix(records, prefix)
    if not matches:
        print(f"no session matching '{prefix}'", file=sys.stderr)
        return None
    if len(matches) > 1:
        print(
            f"ambiguous prefix '{prefix}' — {len(matches)} candidates:",
            file=sys.stderr,
        )
        for r in matches:
            print(f"  {r.sid}  {r.date}  {r.cwd}", file=sys.stderr)
        return None
    return matches[0]


# ---------------------------------------------------------------------------
# forget — privacy retire (record + sidecar + index rows + tombstone)
# ---------------------------------------------------------------------------

def _is_glob(target: str) -> bool:
    """A target is a cwd-glob when it carries an fnmatch metacharacter; anything
    else is a sid-prefix (a sid like ``claude/1a61…`` contains ``/`` but no
    wildcard, so ``/`` alone cannot signal a glob)."""
    return any(ch in target for ch in "*?[")


def _cmd_forget(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="sessionator forget")
    ap.add_argument(
        "target",
        help="a unique sid-prefix (e.g. claude/1a6150d3), or a cwd-glob "
        "carrying a wildcard (e.g. '**/private-notes/**')",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be removed, change nothing",
    )
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return int(e.code) if e.code else 0

    from .index import Index
    from .locking import FileLock, LockBusy
    from .privacy import cwd_excluded
    from .store import Store

    # forget deliberately does NOT reconcile first: it acts on the store as it
    # stands, and the tombstone it writes keeps the sid out on the next ingest.
    try:
        cfg = _load_config_verbose()
        store = Store(cfg)
        records = store.load()
    except Exception as e:  # pragma: no cover - defensive top-level
        print(f"forget error: {e}", file=sys.stderr)
        return 2

    if _is_glob(args.target):
        targets = [
            r for r in records.values() if cwd_excluded(r.cwd, [args.target])
        ]
        targets.sort(key=lambda r: (r.date or "", r.last_active or ""), reverse=True)
        if not targets:
            print(f"no session with cwd matching '{args.target}'", file=sys.stderr)
            return 1
    else:
        rec = _resolve_one(records, args.target)
        if rec is None:
            return 2
        targets = [rec]

    sids = [r.sid for r in targets]

    if args.dry_run:
        print(f"forget --dry-run: would remove {len(sids)} session(s):")
        for r in targets:
            print(f"  {r.sid}  {r.date}  {r.cwd}")
        return 0

    try:
        lock = FileLock(str(cfg.store_lock_path), blocking=True, timeout=600).acquire()
    except LockBusy:
        print("forget error: store is busy, try again", file=sys.stderr)
        return 2
    try:
        live = store.load()
        removed = []
        for r in targets:
            if r.sid in live:
                del live[r.sid]
            store.delete_excerpt(r.sid)  # never touches the harness transcript
            removed.append(r)
        store.write(live)
        tombstones = store.load_tombstones()
        tombstones.update(sids)
        store.write_tombstones(tombstones)
    finally:
        lock.release()

    # Purge the derived index rows immediately (best-effort; a rebuild would
    # drop them anyway on the next query).
    try:
        Index(cfg).delete_sids(sids)
    except Exception:
        pass

    print(f"forgot {len(removed)} session(s) (tombstoned, will not re-ingest):")
    for r in removed:
        print(f"  {r.sid}  {r.date}  {r.cwd}")
    return 0


# ---------------------------------------------------------------------------
# ingest / status / backfill (carried over)
# ---------------------------------------------------------------------------

def _cmd_ingest(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="sessionator ingest")
    ap.add_argument("--no-backfill", action="store_true")
    args = ap.parse_args(argv)

    from .reconcile import reconcile

    try:
        cfg = _load_config_verbose()
        result = reconcile(cfg, kick_backfill=not args.no_backfill)
    except Exception as e:  # pragma: no cover - defensive top-level
        print(f"ingest error: {e}", file=sys.stderr)
        return 2
    print(
        f"scanned {result.scanned} files, "
        f"extracted {result.extracted} sessions "
        f"({result.upserted_new} new, {result.upserted_changed} changed), "
        f"{result.filtered} filtered, {result.excluded} excluded, "
        f"{result.purged} purged, {result.scrubbed} scrubbed"
    )
    if result.by_harness:
        by = ", ".join(f"{k}: {v}" for k, v in sorted(result.by_harness.items()))
        print(f"upserted by harness: {by}")
    if not args.no_backfill:
        print("detached summary backfill kicked (runs in the background)")
    return 0


def _cmd_backfill(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="sessionator _backfill")
    ap.add_argument("--max-batches", type=int, default=None)
    args = ap.parse_args(argv)

    from .summarize import backfill

    try:
        stats = backfill(max_batches=args.max_batches)
    except Exception as e:  # pragma: no cover - defensive top-level
        print(f"backfill error: {e}", file=sys.stderr)
        return 2
    print(
        f"backfill: selected {stats.get('selected', 0)}, "
        f"batches {stats.get('batches', 0)}, "
        f"updated {stats.get('updated', 0)}, "
        f"errors {stats.get('errors', 0)}"
        + (" (already running)" if stats.get("already_running") else "")
    )
    return 0


def _cmd_convert(argv: list[str]) -> int:
    """Hidden one-time migration: fold the legacy daily-report store into the v1
    store (see convert.py). Does not reconcile first — it grafts onto the store
    exactly as the new ingest left it."""
    ap = argparse.ArgumentParser(prog="sessionator convert-legacy")
    ap.add_argument(
        "source",
        nargs="?",
        default=None,
        help="legacy sessions.jsonl (default ~/claude-daily-reports/.store/sessions.jsonl)",
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="report counts, change nothing"
    )
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return int(e.code) if e.code else 0

    from .convert import convert

    try:
        cfg = _load_config_verbose()
        res = convert(cfg, args.source, dry_run=args.dry_run)
    except FileNotFoundError as e:
        print(f"convert error: legacy store not found: {e}", file=sys.stderr)
        return 2
    except Exception as e:  # pragma: no cover - defensive top-level
        print(f"convert error: {e}", file=sys.stderr)
        return 2

    tag = "(dry-run) " if args.dry_run else ""
    print(
        f"{tag}legacy convert: read {res.read}, "
        f"merged {res.merged}, appended {res.appended}, skipped {res.skipped}, "
        f"excluded {res.excluded}, tombstoned {res.tombstoned}, "
        f"unknown-source {res.unknown_source}, parse-errors {res.parse_errors}"
    )
    return 0


def _cmd_status(argv: list[str]) -> int:
    from .index import Index
    from .store import Store

    try:
        cfg = _load_config_verbose()
        _fast_reconcile(cfg)
        store = Store(cfg)
        records = store.load()
    except Exception as e:  # pragma: no cover - defensive top-level
        print(f"status error: {e}", file=sys.stderr)
        return 2

    tombstones = store.load_tombstones()

    print("sessionator status")
    print(f"  config:     {cfg.path}")
    print(f"  data dir:   {cfg.data_dir}")
    print(f"  store:      {cfg.store_path}")
    print(f"  index:      {cfg.index_path}")
    print(f"  exclusions: {cfg.exclusions or '(none)'}  (cwd globs; retro-purged + path-scrubbed)")
    print(f"  tombstones: {len(tombstones)}  (forgotten sids, never re-ingested)")
    print("  sources:")
    for name in ("claude", "codex"):
        src = cfg.sources.get(name)
        if not src:
            continue
        cli = src.cli or "not detected"
        state = "enabled" if src.enabled else "disabled"
        print(f"    {name}: {state} · dir {src.transcript_dir} · cli {cli}")

    by_harness: dict[str, int] = {}
    pending = 0
    errors = 0
    for rec in records.values():
        by_harness[rec.harness] = by_harness.get(rec.harness, 0) + 1
        if rec.summary_state in ("pending", "stale"):
            pending += 1
        elif rec.summary_state == "error":
            errors += 1
    print(f"  records:    {len(records)}", end="")
    if by_harness:
        print(" (" + ", ".join(f"{k}: {v}" for k, v in sorted(by_harness.items())) + ")")
    else:
        print()
    print(f"  summaries:  {pending} pending, {errors} errored")

    try:
        idx = Index(cfg)
        idx.connect()
        engine = (
            "fts5 + trigram"
            if idx.has_tri
            else ("fts5 (porter only)" if idx.has_fts else "linear scan (no fts5)")
        )
        idx.close()
    except Exception as e:
        engine = f"unavailable ({e})"
    print(f"  search:     {engine}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
