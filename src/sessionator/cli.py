"""Command-line entry point for sessionator.

Product verbs (T-002), search implicit:

* ``sessionator <terms…> [filters]`` — bare invocation IS search (the 90% case).
* ``sessionator show <sid-prefix>`` — full record + transcript tail + resume line.
* ``sessionator resume <sid-prefix>`` — exactly one line: the resume invocation.
* ``sessionator ingest`` — manual reconcile.
* ``sessionator status`` — config/sources/index health (doubles as doctor).
* ``sessionator forget <sid|pattern>`` — privacy retire (stub here).
* ``sessionator summarize <sid-prefix>`` — summarize one session now.
* ``sessionator setup codex|status`` — install the Codex capture hooks, or report
  how capture is wired up on this machine.

Query verbs run a fast, non-blocking reconcile first (deterministic inline pass +
detached summary backfill), so a query always sees the just-finished session and
never waits on summaries. ``setup``, ``forget`` and the hidden ``_backfill`` /
``_hook_worker`` do not: configuring or retiring is not querying.

``ingest --hook`` is the harness hook entry point and is special in every way
that matters — it never prints, never exits non-zero, and never reaches argparse.
See ``hooks.py``.

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
    "search", "show", "resume", "ingest", "status", "forget", "summarize",
    "setup", "_backfill", "_hook_worker", "convert-legacy",
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

    # `ingest --hook` is the harness hook entry point and is handled BEFORE
    # argparse: it must never write to stdout (Claude Code parses a hook's
    # stdout) and must never exit non-zero (a failing hook is a user-visible
    # error in the middle of their session). Everything it could complain
    # about — bad flags, no stdin, an unwritable data dir — is silently a
    # no-op instead.
    if argv[0] == "ingest" and "--hook" in argv[1:]:
        return _hook_ingest()

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
    if command == "_hook_worker":
        return _cmd_hook_worker(rest)
    if command == "setup":
        return _cmd_setup(rest)
    if command == "forget":
        return _cmd_forget(rest)
    if command == "summarize":
        return _cmd_summarize(rest)
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
        "       sessionator summarize <sid-prefix>  summarize one session now\n"
        "       sessionator setup codex|status      install Codex capture hooks,\n"
        "                                           or report how capture is wired\n"
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
# summarize — summarize one session now
# ---------------------------------------------------------------------------

def _cmd_summarize(argv: list[str]) -> int:
    """Summarize one session on demand.

    Segments are normally cut by hooks; this is the manual cut. It forces a
    segment over everything not yet summarized (or, when nothing is new, over
    the whole excerpt) and runs the summarizer in the foreground, because a user
    who asked for a summary is waiting for one. ``--no-wait`` cuts the segment
    and leaves it to the detached backfill.
    """
    ap = argparse.ArgumentParser(prog="sessionator summarize")
    ap.add_argument("sid_prefix", help="unique sid prefix (e.g. claude/d78b1004)")
    ap.add_argument(
        "--no-wait",
        action="store_true",
        help="cut the segment and let the background backfill summarize it",
    )
    ap.add_argument("--no-reconcile", action="store_true", help=argparse.SUPPRESS)
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return int(e.code) if e.code else 0

    from .locking import FileLock, LockBusy
    from .reconcile import kick_detached_backfill, transcript_size
    from .render import _SUMMARY_ORDER
    from .segments import (
        append_segment, count_turns, rollup_state, slice_turns, split_turns,
    )
    from .store import Store
    from .summarize import backfill

    try:
        cfg = _load_config_verbose()
        if not args.no_reconcile:
            _fast_reconcile(cfg)
        store = Store(cfg)
        records = store.load()
    except Exception as e:  # pragma: no cover - defensive top-level
        print(f"summarize error: {e}", file=sys.stderr)
        return 2

    rec = _resolve_one(records, args.sid_prefix)
    if rec is None:
        return 2
    sid = rec.sid

    if cfg.summarizer_cli(rec.harness) is None:
        print(
            "summarize error: no summarizer CLI found — install the claude or "
            f"codex CLI, or set [sources.*].cli in {cfg.path}",
            file=sys.stderr,
        )
        return 2

    # Cut the manual segment under the store lock, so a concurrent reconcile or
    # backfill cannot interleave with the read-modify-write.
    try:
        lock = FileLock(str(cfg.store_lock_path), blocking=True, timeout=600).acquire()
    except LockBusy:
        print("summarize error: store is busy, try again", file=sys.stderr)
        return 2
    try:
        live = store.load()
        rec = live.get(sid)
        if rec is None:
            print(f"no session matching '{args.sid_prefix}'", file=sys.stderr)
            return 2
        excerpt = store.read_excerpt(rec)
        segment = append_segment(
            rec,
            event="manual",
            trigger="user",
            turn_count=count_turns(excerpt),
            size=transcript_size(rec),
            force=True,
        )
        if segment is not None:
            text = slice_turns(
                split_turns(excerpt), segment["start"], segment["end"]
            )
            store.write_segment_excerpt(rec, segment["seq"], text)
            rec.summary_state = rollup_state(rec)
            store.write(live)
    finally:
        lock.release()

    if segment is None:
        print(f"nothing to summarize for {sid} (empty excerpt)", file=sys.stderr)
        return 1

    if args.no_wait:
        kick_detached_backfill(cfg, only_sid=sid)
    else:
        try:
            backfill(cfg, only_sid=sid)
        except Exception as e:  # pragma: no cover - defensive
            print(f"summarize warning: {e}", file=sys.stderr)

    rec = Store(cfg).load().get(sid)
    if rec is None:  # pragma: no cover - defensive
        return 2
    summary = rec.summary or {}
    print(f"● {sid}")
    for key, label in _SUMMARY_ORDER:
        print(f"  {label}: {summary.get(key) or '-'}")
    if not any(summary.values()):
        print(
            f"  (summary still {rec.summary_state}"
            + (" — running in the background)" if args.no_wait else ")"),
            file=sys.stderr,
        )
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
    ap.add_argument("--sid", default=None, help="restrict the pass to one sid")
    args = ap.parse_args(argv)

    from .summarize import backfill

    try:
        stats = backfill(max_batches=args.max_batches, only_sid=args.sid)
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


# ---------------------------------------------------------------------------
# setup — install the Codex hooks, or report how capture is wired
# ---------------------------------------------------------------------------

def _cmd_setup(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="sessionator setup",
        description="Install the Codex capture hooks, or report capture status.",
    )
    ap.add_argument(
        "target",
        choices=["codex", "status"],
        help="codex: merge the hooks into $CODEX_HOME/hooks.json; "
        "status: report how capture is wired up on this machine",
    )
    ap.add_argument(
        "--remove", action="store_true", help="remove our hooks instead (codex)"
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="print the change, write nothing"
    )
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return int(e.code) if e.code else 0

    # setup deliberately does NOT reconcile: it is a machine-configuration verb,
    # and making it scan every transcript first would be a surprise.
    if args.target == "status":
        return _setup_status()
    return _setup_codex(remove=args.remove, dry_run=args.dry_run)


def _setup_codex(*, remove: bool, dry_run: bool) -> int:
    from . import setup_hooks as sh

    path = sh.hooks_path()
    try:
        cli_path = sh.resolve_cli()
        command = sh.hook_command(cli_path)
        existing = sh.read_hooks(path)
    except sh.SetupError as e:
        print(f"setup error: {e}", file=sys.stderr)
        return 2

    if remove:
        if existing is None:
            print(f"nothing to remove — {path} does not exist")
            return 0
        result = sh.unmerge(existing, command)
        if not result.changed:
            print(f"nothing to remove — no sessionator hooks in {path}")
            return 0
        delete = sh.is_empty(result.data)
        for line in result.changes:
            print(f"  {line}")
        if dry_run:
            print(
                f"(dry-run) would {'delete' if delete else 'rewrite'} {path}; "
                "nothing was written"
            )
            if not delete:
                print(sh.render(result.data))
            return 0
        try:
            if delete:
                path.unlink()
                print(f"removed {path} (nothing else was in it)")
            else:
                sh.write_hooks(path, result.data)
                print(f"updated {path}")
        except OSError as e:
            print(f"setup error: cannot write {path}: {e}", file=sys.stderr)
            return 2
        print("Your own hooks in config.toml were not touched.")
        return 0

    result = sh.merge(existing, command)
    print(f"sessionator hook command: {command}")
    if not result.changed:
        print(f"{path} is already up to date — nothing written")
        return 0
    for line in result.changes:
        print(f"  {line}")
    if dry_run:
        print(f"(dry-run) would write {path}:")
        print(sh.render(result.data))
        return 0
    try:
        sh.write_hooks(path, result.data)
    except OSError as e:
        print(f"setup error: cannot write {path}: {e}", file=sys.stderr)
        return 2
    print(f"wrote {path}")
    print(sh.TRUST_INSTRUCTIONS)
    return 0


def _setup_status() -> int:
    from . import setup_hooks as sh

    try:
        cfg = _load_config_verbose()
    except Exception as e:  # pragma: no cover - defensive top-level
        print(f"setup status error: {e}", file=sys.stderr)
        return 2

    print("sessionator capture status")
    try:
        cli_path = sh.resolve_cli()
        print(f"  cli:        {cli_path}")
    except sh.SetupError:
        cli_path = None
        print("  cli:        not found on PATH (hooks cannot be installed)")

    claude_hits = sh.find_claude_hooks()
    if claude_hits:
        print("  claude:     hook found in " + ", ".join(str(p) for p in claude_hits))
    else:
        print(
            "  claude:     no sessionator hook found "
            "(install the plugin: /plugin install sessionator@sessionator)"
        )

    path = sh.hooks_path()
    command = sh.hook_command(cli_path) if cli_path else None
    try:
        data = sh.read_hooks(path)
    except sh.SetupError as e:
        data = None
        print(f"  codex:      {path} unreadable — {e.args[0].splitlines()[0]}")
    else:
        if data is None:
            print(f"  codex:      {path} absent (run `sessionator setup codex`)")
        else:
            ours = sh.find_ours(data, command) if command else {}
            if ours:
                print(f"  codex:      {', '.join(sorted(ours))} in {path}")
            else:
                print(f"  codex:      no sessionator hooks in {path}")

    foreign = sh.count_config_toml_hooks()
    print(
        f"  config.toml: {foreign} hook table(s) — yours, never read or written "
        "by setup"
    )
    print(f"  spool:      {sh.spool_depth(cfg)} pending payload(s)")
    models = ", ".join(
        f"{name} via {(cfg.summarize.get(name) or {}).get('model') or '?'}"
        for name in ("claude", "codex")
    )
    print(f"  summarizer: prefer {cfg.summarize_prefer} · {models}")
    return 0


# ---------------------------------------------------------------------------
# hook entry points (never print: stdout belongs to the harness protocol)
# ---------------------------------------------------------------------------

def _hook_ingest() -> int:
    """``sessionator ingest --hook``: spool stdin, detach a worker, exit 0.

    Called from inside a harness hook, so it is silent and total: no stdout, no
    stderr, no non-zero exit, whatever goes wrong. The real work happens in the
    detached ``_hook_worker``."""
    try:
        from .hooks import spool_and_detach

        raw = sys.stdin.buffer.read() if sys.stdin is not None else b""
        spool_and_detach(raw)
    except Exception:
        pass
    return 0


def _cmd_hook_worker(argv: list[str]) -> int:
    """Hidden: consume one spooled hook payload. Detached, output-less, exit 0."""
    try:
        from .hooks import run_worker

        if argv:
            run_worker(argv[0])
    except Exception:
        pass
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
    segmented = 0
    segment_total = 0
    segment_pending = 0
    for rec in records.values():
        by_harness[rec.harness] = by_harness.get(rec.harness, 0) + 1
        if rec.summary_state in ("pending", "stale", "partial"):
            pending += 1
        elif rec.summary_state == "error":
            errors += 1
        segs = rec.summary_segments or []
        if segs:
            segmented += 1
            segment_total += len(segs)
            segment_pending += sum(1 for s in segs if s.get("state") == "pending")
    print(f"  records:    {len(records)}", end="")
    if by_harness:
        print(" (" + ", ".join(f"{k}: {v}" for k, v in sorted(by_harness.items())) + ")")
    else:
        print()
    print(f"  summaries:  {pending} pending, {errors} errored")
    print(
        f"  segments:   {segment_total} in {segmented} session(s), "
        f"{segment_pending} pending"
    )
    models = ", ".join(
        f"{name} via {(cfg.summarize.get(name) or {}).get('model') or '?'}"
        for name in ("claude", "codex")
    )
    print(f"  summarizer: prefer {cfg.summarize_prefer} · {models}")

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
