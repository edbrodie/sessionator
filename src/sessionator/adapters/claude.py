"""Claude Code adapter.

Implements the four-function adapter contract over
``$CLAUDE_CONFIG_DIR/projects`` (else ``~/.claude/projects``). Session files are
``<mangled-cwd>/<uuid>.jsonl``; the sid uuid is the filename stem and is also the
``--resume`` id. Defensive rules (T-007): skip ``/subagents/`` dirs and
``isSidechain`` sessions, skip sessionator's own summarizer transcripts, ignore
unknown message types, and tolerate malformed lines.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from ..schema import Record, empty_summary, make_sid
from . import _common
from ._common import Walker, iter_jsonl

NAME = "claude"


def discover_sources(config) -> list[Path]:
    src = config.sources.get(NAME)
    if not src or not src.enabled or not src.transcript_dir:
        return []
    root = Path(src.transcript_dir)
    return [root] if root.is_dir() else []


def enumerate_sessions(root: Path):
    """Yield (path, mtime, size) for every candidate session file, excluding the
    ``/subagents/`` subtrees (those are not top-level sessions)."""
    for dirpath, dirnames, filenames in os.walk(root):
        if "subagents" in Path(dirpath).parts:
            continue
        for fn in filenames:
            if not fn.endswith(".jsonl"):
                continue
            p = Path(dirpath) / fn
            try:
                st = p.stat()
            except OSError:
                continue
            yield (p, st.st_mtime, st.st_size)


def extract(path, config) -> Record | None:
    path = Path(path)
    w = Walker()
    saw_sidechain = False

    for obj in iter_jsonl(path, w):
        w.tick()

        if isinstance(obj.get("cwd"), str):
            w.set_cwd(obj["cwd"])

        if obj.get("isSidechain") is True:
            saw_sidechain = True

        t = obj.get("type")
        msg = obj.get("message")

        if isinstance(msg, dict):
            content = msg.get("content")
            if isinstance(content, list):
                for blk in content:
                    if not isinstance(blk, dict):
                        continue
                    bt = blk.get("type")
                    if bt == "tool_use":
                        _scan_tool_use(w, blk)
                    elif bt == "tool_result":
                        tuid = blk.get("tool_use_id")
                        for tx in _tr_texts(blk.get("content")):
                            w.add_command_output(tx, call_id=tuid)
                    elif bt == "text":
                        w.add_text(blk.get("text"))

        if t == "user":
            if obj.get("isMeta"):
                continue
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if isinstance(content, str):
                w.add_user(content, ts=obj.get("timestamp"))
        elif t == "assistant":
            if not isinstance(msg, dict):
                continue
            w.set_model(msg.get("model"))
            content = msg.get("content")
            if isinstance(content, list):
                parts = [
                    blk.get("text").strip()
                    for blk in content
                    if isinstance(blk, dict)
                    and blk.get("type") == "text"
                    and isinstance(blk.get("text"), str)
                    and blk.get("text").strip()
                ]
                if parts:
                    w.add_assistant("\n".join(parts))
        # Unknown message types are ignored (T-007 rule 2).

    if saw_sidechain and w.n_user > 0:
        # A sidechain (subagent) transcript, not a top-level session.
        return None

    uuid = path.stem
    fallback_day = _mtime_day(path)
    fields = w.finish(fallback_day=fallback_day)
    if fields is None:
        return None

    # Fallback cwd from the mangled folder name when none was seen inline.
    cwd = fields["cwd"]
    if not cwd:
        cwd = _decode_folder(path.parent.name)
        fields["cwd"] = cwd

    return _to_record(uuid, str(path), fields)


def _to_record(uuid, transcript_path, fields) -> Record:
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
        forked_from=None,
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
        transcript_path=transcript_path,
        summary_state="pending",
        parse_warnings=fields["parse_warnings"],
        # One client writes this format; Codex has several and reads its own
        # originator off the rollout.
        client="claude-code",
    )
    rec.excerpt = fields["excerpt"]
    rec.excerpt_full = fields["excerpt_full"]
    rec.turn_count = fields["turn_count"]
    rec.boundaries = fields["boundaries"]
    return rec


def watermark_key(path) -> str:
    """Identity of a Claude session for the watermark scan: the filename stem,
    which is the session uuid (and the ``--resume`` id). Path-independent, so a
    moved projects dir does not re-extract every session."""
    return f"{NAME}:{Path(path).stem}"


def _tr_texts(cc):
    """Yield text from a tool_result content (string OR list of blocks)."""
    if isinstance(cc, str):
        yield cc
    elif isinstance(cc, list):
        for b in cc:
            if isinstance(b, dict) and b.get("type") == "text":
                txt = b.get("text")
                if isinstance(txt, str):
                    yield txt


def _scan_tool_use(w: Walker, blk):
    name = blk.get("name")
    inp = blk.get("input")
    if not isinstance(inp, dict):
        inp = {}
    if name == "Bash":
        w.add_command(inp.get("command"), call_id=blk.get("id"))
    elif name == "Write":
        w.add_file("C", inp.get("file_path"))
    elif name in ("Edit", "NotebookEdit"):
        w.add_file("M", inp.get("file_path"))
    elif name == "Read":
        w.add_read(inp.get("file_path"))
    elif name == "TodoWrite":
        todos = inp.get("todos")
        if isinstance(todos, str):
            try:
                todos = json.loads(todos)
            except Exception:
                todos = None
        w.set_todos(todos)
    elif name == "Skill":
        w.add_skill(inp.get("skill"))
    elif name in ("Agent", "Task"):
        w.add_subagent(inp.get("subagent_type"))
    elif isinstance(name, str) and name.startswith("mcp__"):
        w.add_mcp(_common.mcp_label(name))


def _mtime_day(path):
    from datetime import datetime
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")
    except OSError:
        return ""


def _decode_folder(folder):
    guess = folder
    if guess.startswith("-"):
        guess = "/" + guess[1:]
    return guess.replace("-", "/")
