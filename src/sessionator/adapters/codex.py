"""Codex CLI adapter.

Implements the four-function adapter contract over ``$CODEX_HOME/sessions``
(else ``~/.codex/sessions``). Rollout files are
``YYYY/MM/DD/rollout-<ISO-ts>-<uuid>.jsonl``.

Defensive rules (T-007 rule 4): the rollout's ``session_meta.payload`` is
authoritative. Only interactive TUI top-level threads are ingested — line 1 must
be a ``session_meta`` with ``originator == "codex-tui"`` and
``thread_source == "user"``; headless ``codex exec`` and subagent threads are
skipped. The sid uuid is the rollout's top-level ``id`` (fork-unique), NOT the
fork-shared ``session_id``; ``forked_from_id`` lineage is captured.
``history.jsonl`` is never read.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from ..schema import Record, empty_summary, make_sid
from ._common import Walker, iter_jsonl

NAME = "codex"


def discover_sources(config) -> list[Path]:
    src = config.sources.get(NAME)
    if not src or not src.enabled or not src.transcript_dir:
        return []
    root = Path(src.transcript_dir)
    return [root] if root.is_dir() else []


def enumerate_sessions(root: Path):
    """Yield (path, mtime, size) for every ``rollout-*.jsonl`` candidate.
    Interactive/subagent filtering happens in ``extract`` via session_meta."""
    for dirpath, dirnames, filenames in os.walk(root):
        for fn in filenames:
            if not (fn.startswith("rollout-") and fn.endswith(".jsonl")):
                continue
            p = Path(dirpath) / fn
            try:
                st = p.stat()
            except OSError:
                continue
            yield (p, st.st_mtime, st.st_size)


def _read_meta(path):
    """Read + validate line 1. Returns the session_meta payload dict for an
    interactive top-level TUI thread, else None."""
    try:
        with open(path, "r", errors="replace") as f:
            first = f.readline()
    except OSError:
        return None
    try:
        meta = json.loads(first)
    except Exception:
        return None
    if not isinstance(meta, dict) or meta.get("type") != "session_meta":
        return None
    mp = meta.get("payload")
    if not isinstance(mp, dict):
        return None
    if mp.get("originator") != "codex-tui" or mp.get("thread_source") != "user":
        return None
    return mp


def extract(path, config) -> Record | None:
    path = Path(path)
    mp = _read_meta(path)
    if mp is None:
        return None

    w = Walker()
    if isinstance(mp.get("cwd"), str):
        w.set_cwd(mp["cwd"])

    uuid = mp.get("id") or _uuid_from_name(path.name)
    if not uuid:
        return None
    forked_from = mp.get("forked_from_id") or mp.get("parent_thread_id") or None

    # The session_meta line is already consumed by _read_meta; re-open and skip
    # it here so iter_jsonl's warning counting covers the body lines.
    with open(path, "r", errors="replace") as f:
        f.readline()  # skip session_meta
        for obj in _iter_body(f, w):
            _handle(w, obj)

    fallback_day = _mtime_day(path)
    fields = w.finish(fallback_day=fallback_day)
    if fields is None:
        return None

    rec = Record(
        sid=make_sid(NAME, uuid),
        harness=NAME,
        native_id=uuid,
        date=fields["date"],
        cwd=fields["cwd"],
        last_active=fields["last_active"],
        model=fields["model"],
        repo=fields["repo"],
        branch=fields["branch"],
        forked_from=forked_from,
        files=fields["files"],
        commits=fields["commits"],
        prs=fields["prs"],
        keywords=fields["keywords"],
        skills=fields["skills"],
        subagents=fields["subagents"],
        mcp=fields["mcp"],
        open_todos=fields["open_todos"],
        tests=fields["tests"],
        resolved=fields["resolved"],
        summary=empty_summary(),
        transcript_path=str(path),
        summary_state="pending",
        parse_warnings=fields["parse_warnings"],
    )
    rec.excerpt = fields["excerpt"]
    return rec


def _iter_body(f, w):
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            w.note_warning()
            continue
        if isinstance(obj, dict):
            yield obj


def _handle(w: Walker, obj):
    w.tick()
    t = obj.get("type")
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        return
    pt = payload.get("type")

    if t == "turn_context":
        w.set_model(payload.get("model"))
        return

    if t == "event_msg":
        if pt == "user_message":
            w.add_user(payload.get("message"), ts=obj.get("timestamp"))
        elif pt == "agent_message":
            w.add_assistant(payload.get("message"))
        elif pt == "patch_apply_end":
            if payload.get("success"):
                w.add_patch_changes(payload.get("changes"))
        elif pt == "mcp_tool_call_end":
            inv = payload.get("invocation")
            if isinstance(inv, dict):
                w.add_mcp(inv.get("server"))
        return

    if t == "response_item":
        if pt == "function_call":
            _handle_function_call(w, payload)
        elif pt == "function_call_output":
            out = payload.get("output")
            if isinstance(out, str):
                w.add_command_output(out, call_id=payload.get("call_id"))
    # Unknown types ignored (T-007 rule 2).


def _handle_function_call(w: Walker, payload):
    name = payload.get("name")
    args = payload.get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            args = {}
    if not isinstance(args, dict):
        args = {}
    if name == "exec_command":
        w.add_command(args.get("cmd"), call_id=payload.get("call_id"))
    elif name == "spawn_agent":
        w.add_subagent(args.get("agent_type"))
    elif name == "update_plan":
        plan = args.get("plan")
        if isinstance(plan, list):
            todos = [
                {"content": st.get("step"), "status": st.get("status")}
                for st in plan
                if isinstance(st, dict)
            ]
            w.set_todos(todos)


def _uuid_from_name(name):
    # rollout-<ISO-ts>-<uuid>.jsonl ; uuid is the 5-group tail.
    stem = name[len("rollout-"):-len(".jsonl")] if name.endswith(".jsonl") else name
    parts = stem.split("-")
    if len(parts) >= 5:
        return "-".join(parts[-5:])
    return stem


def _mtime_day(path):
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")
    except OSError:
        return ""
