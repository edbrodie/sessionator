"""Detached backfill: batch-summarize records that lack a fresh summary.

Entry point for the hidden ``_backfill`` subcommand the reconcile path spawns.
It is self-terminating and idempotent: a single-instance lock makes concurrent
kicks harmless (a second backfill exits at once).

Flow:

1. Take the backfill lock non-blocking; exit if another backfill holds it.
2. Under the store-write lock: load records, select the work set (summary_state
   in pending/stale/error, with a readable excerpt), snapshot the work, release.
3. Group by the CLI that will run each session (same-harness, else fallback),
   batch ~8 with the ``@@S<n>@@`` marker pattern, and call the CLI once per batch
   with the private-stripped, capped excerpt as input.
4. Under the store-write lock: reload, apply parsed 5-field summaries + resolved
   to records that still need them, write.

Summarizer errors leave a record summary-less (retried next backfill) and never
propagate. IMPORTANT auth/stdin handling: the ``claude -p`` call unsets the
auth-conflicting env vars and disables session persistence (so it does not
self-pollute ~/.claude/projects); the ``codex exec`` call closes stdin.
"""

from __future__ import annotations

import os
import re
import subprocess

from .adapters._common import SUMMARIZER_SENTINEL
from .config import Config, load as load_config
from .locking import FileLock, LockBusy
from .schema import RESOLVED_VALUES, SUMMARY_FIELDS
from .store import Store

BATCH_SIZE = 8
INPUT_CAP = 6000  # per-session excerpt chars fed to the summarizer
WORK_STATES = ("pending", "stale", "error")

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


def backfill(config: Config | None = None, *, max_batches: int | None = None) -> dict:
    """Run one backfill pass. Returns a small stats dict. Never raises for
    summarizer failures."""
    if config is None:
        config = load_config()
    store = Store(config)
    stats = {"selected": 0, "batches": 0, "updated": 0, "errors": 0, "skipped_no_cli": 0}

    # Single-instance guard.
    try:
        inst_lock = FileLock(str(config.backfill_lock_path), blocking=False).acquire()
    except LockBusy:
        stats["already_running"] = True
        return stats

    try:
        work = _select_work(config, store)
        stats["selected"] = len(work)
        if not work:
            return stats

        groups = _group_by_cli(config, work)
        results: dict[str, dict] = {}  # sid -> {fields}
        errored: set[str] = set()
        batches_run = 0
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
        stats["updated"] = _apply(config, store, results, errored)
        stats["errors"] = len(errored - set(results))
        return stats
    finally:
        inst_lock.release()


def _select_work(config, store):
    """Snapshot [(sid, excerpt_text)] for records needing a summary. Runs under
    the store-write lock (read-modify-nothing, but keeps a consistent view)."""
    lock = FileLock(str(config.store_lock_path), blocking=True, timeout=600).acquire()
    try:
        records = store.load()
    finally:
        lock.release()
    work = []
    for sid, rec in records.items():
        if rec.summary_state not in WORK_STATES:
            continue
        excerpt = store.read_excerpt(rec)
        if not excerpt.strip():
            continue
        work.append((sid, rec.harness, excerpt))
    return work


def _group_by_cli(config, work):
    """Group work items by the CLI that will summarize them. Items lacking any
    installed CLI are dropped (left pending for a machine that has one)."""
    groups: dict[tuple, list] = {}
    for sid, harness, excerpt in work:
        cli = config.summarizer_cli(harness)
        if cli is None:
            continue
        groups.setdefault(cli, []).append((sid, excerpt))
    return groups


def _cap_excerpt(text: str) -> str:
    if len(text) <= INPUT_CAP:
        return text
    head = text[: INPUT_CAP * 2 // 3]
    tail = text[-INPUT_CAP // 3:]
    return head + "\n...[trimmed]...\n" + tail


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
    model = settings.get("model") or "opus-4.8"
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
    model = settings.get("model") or "gpt-5.6-luna"
    reasoning = settings.get("reasoning") or "high"
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


def _apply(config, store, results, errored) -> int:
    """Write parsed summaries back under the store-write lock, applying only to
    records that still need a summary (a concurrent reconcile may have changed
    them)."""
    if not results and not errored:
        return 0
    lock = FileLock(str(config.store_lock_path), blocking=True, timeout=600).acquire()
    try:
        records = store.load()
        updated = 0
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
        if updated or errored:
            store.write(records)
        return updated
    finally:
        lock.release()
