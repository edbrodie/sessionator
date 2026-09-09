"""Detached backfill: batch-summarize records that lack a fresh summary.

Entry point for the hidden ``_backfill`` subcommand the reconcile path spawns.
It is self-terminating and idempotent: a single-instance lock makes concurrent
kicks harmless (a second backfill exits at once).

Two kinds of work share the pass:

* **Segment rollups** — one call per pending ``summary_segments`` entry, folding
  just that slice into the record's running five-field summary. This is how a
  long, compacting session is summarized without re-reading it whole every time.
* **Legacy whole-record batches** — a record with no segments (a plain ingest
  never opens one) is summarized from its whole excerpt, ~8 sessions per call
  with the ``@@S<n>@@`` marker pattern.

Flow:

1. Take the backfill lock non-blocking; exit if another backfill holds it.
2. Under the store-write lock: load records, select the work set (pending
   segments first, then records whose state is pending/stale/error/partial with a
   readable excerpt), snapshot the work, release.
3. Run segment rollups one call each, then group the rest by the CLI that will
   run them (see ``[summarize].prefer``) and batch.
4. Under the store-write lock: reload, apply parsed 5-field summaries + resolved
   to records/segments that still need them, write.

Summarizer errors leave a record summary-less (retried next backfill) and never
propagate. IMPORTANT auth/stdin handling: the ``claude -p`` call unsets the
auth-conflicting env vars and disables session persistence (so it does not
self-pollute ~/.claude/projects); the ``codex exec`` call closes stdin.
"""

from __future__ import annotations

import os
import re
import subprocess

from dataclasses import dataclass

from . import segments as seg
from .adapters._common import SUMMARIZER_SENTINEL
from .config import DEFAULT_SUMMARIZE, Config, load as load_config
from .locking import FileLock, LockBusy
from .render import _SUMMARY_ORDER
from .schema import RESOLVED_VALUES, SUMMARY_FIELDS
from .segments import INPUT_CAP, cap_text
from .store import Store

BATCH_SIZE = 8
WORK_STATES = ("pending", "stale", "error", "partial")

_MARKER_RX = re.compile(r"@@(S\d+)@@")

# Output label -> summary field.
_LABELS = {
    "Asked": "asked",
    "Learned": "learned",
    "Completed": "completed",
    "Left off": "left_off",
    "Next steps": "next_steps",
    "Resolved": "resolved",
}
_LABEL_RX = {
    label: re.compile(r"-\s*\*\*" + re.escape(label) + r":\*\*\s*(.+)", re.IGNORECASE)
    for label in _LABELS
}


@dataclass
class _Work:
    """One summarizer call's worth of input. ``seq`` is the segment being rolled
    up, or None for a legacy whole-record summary."""

    sid: str
    harness: str
    text: str
    seq: int | None = None
    event: str | None = None
    prev_summary: dict | None = None


def backfill(
    config: Config | None = None, *, max_batches: int | None = None,
    only_sid: str | None = None, wait: float | None = None,
) -> dict:
    """Run one backfill pass. Returns a small stats dict. Never raises for
    summarizer failures. ``only_sid`` restricts the pass to one session — the
    hook path, which knows what just changed and must not sweep the store.
    ``wait`` makes the single-instance guard block up to that many seconds
    instead of yielding: a detached hook worker would otherwise lose its one
    segment to whatever sweep happens to be running and leave it pending until
    the next kick."""
    if config is None:
        config = load_config()
    store = Store(config)
    stats = {
        "selected": 0, "batches": 0, "updated": 0, "errors": 0,
        "skipped_no_cli": 0, "segments": 0,
    }

    # Single-instance guard.
    try:
        if wait:
            inst_lock = FileLock(
                str(config.backfill_lock_path), blocking=True, timeout=float(wait),
            ).acquire()
        else:
            inst_lock = FileLock(str(config.backfill_lock_path), blocking=False).acquire()
    except LockBusy:
        stats["already_running"] = True
        return stats

    try:
        work = _select_work(config, store, only_sid=only_sid)
        stats["selected"] = len(work)
        if not work:
            return stats

        seg_items = [w for w in work if w.seq is not None]
        legacy = [w for w in work if w.seq is None]

        seg_results: dict[tuple, dict] = {}
        seg_errors: set[tuple] = set()
        rolled: dict[str, dict] = {}  # sid -> summary as of the last applied cut
        batches_run = 0
        for item in seg_items:
            if max_batches is not None and batches_run >= max_batches:
                break
            cli = config.summarizer_cli(item.harness)
            if cli is None:
                stats["skipped_no_cli"] += 1
                continue
            batches_run += 1
            stats["segments"] += 1
            # Later segments of the same session fold into the result of the
            # earlier ones, not into the snapshot taken before this pass.
            prev = rolled.get(item.sid, item.prev_summary)
            fields = _run_segment(config, cli[0], cli[1], item, prev)
            if fields:
                seg_results[(item.sid, item.seq)] = fields
                rolled[item.sid] = fields["summary"]
            else:
                seg_errors.add((item.sid, item.seq))

        groups = _group_by_cli(config, legacy)
        results: dict[str, dict] = {}  # sid -> {fields}
        errored: set[str] = set()
        for (cli_harness, cli_path), items in groups.items():
            for i in range(0, len(items), BATCH_SIZE):
                if max_batches is not None and batches_run >= max_batches:
                    break
                chunk = items[i:i + BATCH_SIZE]
                batches_run += 1
                parsed = _run_batch(config, cli_harness, cli_path, chunk)
                for sid, fields in parsed.items():
                    if fields:
                        results[sid] = fields
                    else:
                        errored.add(sid)
                # Any chunk member with no parse result is an error to retry.
                for sid, _excerpt in chunk:
                    if sid not in parsed:
                        errored.add(sid)
            if max_batches is not None and batches_run >= max_batches:
                break

        stats["batches"] = batches_run
        stats["updated"] = _apply(
            config, store, results, errored,
            segment_results=seg_results, segment_errors=seg_errors,
        )
        stats["errors"] = len(errored - set(results)) + len(seg_errors)
        return stats
    finally:
        inst_lock.release()


def _select_work(config, store, *, only_sid: str | None = None):
    """Snapshot the work set: pending segments first (ordered by sid then seq),
    then legacy whole-record work. Runs under the store-write lock (it mutates
    nothing, but a consistent view keeps the applied result meaningful)."""
    lock = FileLock(str(config.store_lock_path), blocking=True, timeout=600).acquire()
    try:
        records = store.load()
    finally:
        lock.release()

    seg_work, legacy = [], []
    for sid in sorted(records):
        rec = records[sid]
        if only_sid and sid != only_sid:
            continue
        pending = seg.pending(rec.summary_segments)
        if pending:
            turns = None
            for segment in sorted(pending, key=lambda s: int(s.get("seq") or 0)):
                text = store.read_segment_excerpt(rec, segment["seq"])
                if not text.strip():
                    # No sidecar (an older record, or a slice that was empty at
                    # cut time): re-slice the main excerpt instead.
                    if turns is None:
                        turns = seg.split_turns(store.read_excerpt(rec))
                    text = seg.slice_turns(turns, segment["start"], segment["end"])
                if not text.strip():
                    continue
                seg_work.append(_Work(
                    sid=sid,
                    harness=rec.harness,
                    text=text,
                    seq=segment["seq"],
                    event=segment.get("event"),
                    prev_summary=dict(rec.summary or {}),
                ))
            # A segmented record is summarized only through its segments; the
            # whole-record path would undo the rollup.
            continue
        if rec.summary_state not in WORK_STATES:
            continue
        excerpt = store.read_excerpt(rec)
        if not excerpt.strip():
            continue
        legacy.append(_Work(sid=sid, harness=rec.harness, text=excerpt))
    return seg_work + legacy


def _group_by_cli(config, work):
    """Group work items by the CLI that will summarize them. Items lacking any
    installed CLI are dropped (left pending for a machine that has one)."""
    groups: dict[tuple, list] = {}
    for item in work:
        cli = config.summarizer_cli(item.harness)
        if cli is None:
            continue
        groups.setdefault(cli, []).append((item.sid, item.text))
    return groups


def _cap_excerpt(text: str) -> str:
    return cap_text(text, INPUT_CAP)


def build_prompt(chunk) -> str:
    """One prompt for a batch. ``chunk`` is [(sid, excerpt)]; each session is a
    ``@@S<n>@@`` block and the model echoes the marker before its six labelled
    lines, so the response splits back per session."""
    head = (
        f"{SUMMARIZER_SENTINEL}\n"
        "You summarize developer coding-agent sessions. Each session is "
        "introduced by a marker line like '@@S1@@'. For EACH session, first echo "
        "its marker line EXACTLY on its own line, then output EXACTLY these six "
        "markdown lines and nothing else:\n"
        "- **Asked:** what the user set out to do\n"
        "- **Learned:** key findings or facts uncovered during the work\n"
        "- **Completed:** what actually got done\n"
        "- **Left off:** the state at the end / where work stopped\n"
        "- **Next steps:** concrete follow-ups, or 'None'\n"
        "- **Resolved:** exactly one of open, done, or unknown\n"
        "Base every field ONLY on that session's transcript. Be concise and "
        "specific. Do all sessions in order. No preamble, no commentary.\n"
    )
    blocks = []
    for idx, (_sid, excerpt) in enumerate(chunk, start=1):
        blocks.append(f"@@S{idx}@@\n{_cap_excerpt(excerpt)}")
    return head + "\n" + "\n\n===\n\n".join(blocks) + "\n"


def build_rollup_prompt(prev_summary, segment_text: str, event: str | None) -> str:
    """One prompt folding a single new segment into the running summary.

    The output contract is deliberately identical to the batch prompt's — one
    ``@@S1@@`` echo then the six labelled lines — so ``_split_by_marker`` and
    ``_parse_fields`` handle both without a second parser.
    """
    current = _render_summary(prev_summary) or "(none yet)"
    label = f" (cut at: {event})" if event else ""
    head = (
        f"{SUMMARIZER_SENTINEL}\n"
        "You maintain a running summary of ONE developer coding-agent session. "
        "Below is the summary so far and a NEW, LATER excerpt of the same "
        "session. Update the summary so it describes the whole session, "
        "including the new excerpt: keep what still holds, correct what the new "
        "excerpt supersedes, and add what it adds. First echo the line '@@S1@@' "
        "EXACTLY on its own line, then output EXACTLY these six markdown lines "
        "and nothing else:\n"
        "- **Asked:** what the user set out to do\n"
        "- **Learned:** key findings or facts uncovered during the work\n"
        "- **Completed:** what actually got done\n"
        "- **Left off:** the state at the end / where work stopped\n"
        "- **Next steps:** concrete follow-ups, or 'None'\n"
        "- **Resolved:** exactly one of open, done, or unknown\n"
        "Base every field ONLY on this session. Be concise and specific. No "
        "preamble, no commentary.\n"
    )
    return (
        head
        + "\n=== SUMMARY SO FAR ===\n"
        + current
        + f"\n\n=== NEW EXCERPT{label} ===\n"
        + _cap_excerpt(segment_text)
        + "\n"
    )


def _render_summary(summary) -> str:
    """The five fields as the same labelled lines the model must emit back."""
    if not isinstance(summary, dict):
        return ""
    lines = [
        f"- **{label}:** {summary.get(key)}"
        for key, label in _SUMMARY_ORDER
        if summary.get(key)
    ]
    return "\n".join(lines)


def _run_segment(config, cli_harness, cli_path, item, prev_summary) -> dict | None:
    """Roll one segment into the running summary. Returns parsed fields, or None
    on any CLI/parse failure (the segment stays pending and is retried)."""
    prompt = build_rollup_prompt(prev_summary, item.text, item.event)
    raw = _invoke_cli(config, cli_harness, cli_path, prompt)
    if not raw:
        return None
    per_token = _split_by_marker(raw)
    # A model that forgot the echo still produces the six lines.
    body = per_token.get("S1") or raw
    return _parse_fields(body)


def _run_batch(config, cli_harness, cli_path, chunk) -> dict:
    """Run one batch through a CLI and return {sid: fields_dict}. On CLI failure
    returns {} (whole batch retried later)."""
    prompt = build_prompt(chunk)
    raw = _invoke_cli(config, cli_harness, cli_path, prompt)
    if not raw:
        return {}
    per_token = _split_by_marker(raw)
    out = {}
    for idx, (sid, _excerpt) in enumerate(chunk, start=1):
        body = per_token.get(f"S{idx}")
        out[sid] = _parse_fields(body) if body else None
    return out


def _invoke_cli(config, cli_harness, cli_path, prompt) -> str | None:
    if cli_harness == "claude":
        return _invoke_claude(config, cli_path, prompt)
    if cli_harness == "codex":
        return _invoke_codex(config, cli_path, prompt)
    return None


def _invoke_claude(config, cli_path, prompt) -> str | None:
    settings = config.summarize.get("claude", {})
    model = settings.get("model") or DEFAULT_SUMMARIZE["claude"]["model"]
    env = os.environ.copy()
    # Use interactive session credentials, not a stale/empty API key.
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    base = [
        cli_path, "-p",
        "--strict-mcp-config",
        "--settings", '{"enabledPlugins":{}}',
        "--no-session-persistence",
    ]
    for args in ([*base, "--model", model, prompt], [*base, prompt]):
        out = _run(args, env=env)
        if out and "@@S" in out:
            return out
    return out or None


def _invoke_codex(config, cli_path, prompt) -> str | None:
    settings = config.summarize.get("codex", {})
    model = settings.get("model") or DEFAULT_SUMMARIZE["codex"]["model"]
    reasoning = settings.get("reasoning") or DEFAULT_SUMMARIZE["codex"]["reasoning"]
    attempts = [
        [cli_path, "exec", "-m", model, "-c", f"model_reasoning_effort={reasoning}", prompt],
        [cli_path, "exec", "-m", model, prompt],
        [cli_path, "exec", prompt],
    ]
    for args in attempts:
        out = _run(args, env=os.environ.copy())
        if out and "@@S" in out:
            return out
    return out or None


def _run(args, env) -> str | None:
    try:
        proc = subprocess.run(
            args,
            env=env,
            stdin=subprocess.DEVNULL,  # codex exec waits on stdin EOF otherwise
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=300,
            text=True,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _split_by_marker(raw: str) -> dict:
    parts = _MARKER_RX.split(raw)
    res = {}
    it = iter(parts[1:])
    for tok, body in zip(it, it):
        if tok not in res:
            res[tok] = body
    return res


def _parse_fields(body: str) -> dict | None:
    found = {}
    for line in (body or "").splitlines():
        s = line.strip()
        if not s:
            continue
        for label, field in _LABELS.items():
            if field in found:
                continue
            m = _LABEL_RX[label].match(s)
            if m:
                found[field] = m.group(1).strip()
    if not any(found.get(f) for f in SUMMARY_FIELDS):
        return None
    resolved = (found.get("resolved") or "").strip().lower()
    summary = {f: found.get(f, "") for f in SUMMARY_FIELDS}
    result = {"summary": summary}
    if resolved in RESOLVED_VALUES:
        result["resolved"] = resolved
    return result


def _apply(
    config, store, results, errored, *, segment_results=None, segment_errors=None,
) -> int:
    """Write parsed summaries back under the store-write lock, applying only to
    records/segments that still need one (a concurrent reconcile may have changed
    them). A segment rollup writes the record's ``summary`` too: the rollup IS
    the whole-session summary as of that cut, and the segment keeps its own copy
    as the audit trail."""
    segment_results = segment_results or {}
    segment_errors = segment_errors or set()
    if not results and not errored and not segment_results and not segment_errors:
        return 0
    lock = FileLock(str(config.store_lock_path), blocking=True, timeout=600).acquire()
    try:
        records = store.load()
        updated = 0
        for (sid, seq), fields in sorted(segment_results.items()):
            rec = records.get(sid)
            if rec is None or not seg.mark_done(rec, seq, fields["summary"]):
                continue
            rec.summary = fields["summary"]
            if "resolved" in fields:
                rec.resolved = fields["resolved"]
            rec.summary_state = seg.rollup_state(rec)
            updated += 1
        for sid, seq in sorted(segment_errors):
            rec = records.get(sid)
            if rec is not None and seg.mark_error(rec, seq):
                rec.summary_state = seg.rollup_state(rec)
        for sid, fields in results.items():
            rec = records.get(sid)
            if rec is None or rec.summary_state not in WORK_STATES:
                continue
            rec.summary = fields["summary"]
            if "resolved" in fields:
                rec.resolved = fields["resolved"]
            rec.summary_state = "done"
            updated += 1
        for sid in errored:
            rec = records.get(sid)
            if rec is not None and rec.summary_state in WORK_STATES:
                rec.summary_state = "error"
        if updated or errored or segment_results or segment_errors:
            store.write(records)
        return updated
    finally:
        lock.release()
