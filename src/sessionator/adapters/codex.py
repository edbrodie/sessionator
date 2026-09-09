"""Codex CLI adapter.

Implements the four-function adapter contract over ``$CODEX_HOME/sessions``
(else ``~/.codex/sessions``). Rollout files are
``YYYY/MM/DD/rollout-<ISO-ts>-<uuid>.jsonl``.

Defensive rules (T-007 rule 4): the rollout's ``session_meta.payload`` is
authoritative. Only interactive top-level threads are ingested — line 1 must be a
``session_meta`` with ``thread_source == "user"`` and an ``originator`` that is
not on the machine-driven denylist. The filter is a **denylist, not an
allowlist**: the desktop app writes ``Codex Desktop``, the TUI writes
``codex-tui``, and a future client will write something we have never seen, so an
allowlist silently drops real sessions (it dropped every desktop session until
this was fixed). Only the originators that mean "no human was driving this" —
``codex exec``, subagents, MCP, cloud, automation — are rejected. The originator
is kept on the record as ``client``.

The sid uuid is the rollout's top-level ``id`` (fork-unique), NOT the fork-shared
``session_id``; ``forked_from_id`` lineage is captured. ``history.jsonl`` is
never read. Archiving a thread **moves** its rollout from ``sessions/`` to a flat
``archived_sessions/``, so both dirs are enumerated and the watermark is keyed on
the rollout uuid rather than its path.

Two rollout dialects are read by the same walk:

* **legacy** (codex-cli ~0.4x–0.1xx): turns arrive as ``event_msg`` payloads
  ``user_message`` / ``agent_message``, shell as ``response_item`` /
  ``function_call`` ``exec_command``, file edits as ``patch_apply_end``.
* **current** (codex-cli 0.15x, CLI *and* the desktop app): turns arrive as
  ``response_item`` / ``message`` items whose ``content`` is a list of
  ``{"type": "input_text"|"output_text", "text": …}`` parts with a ``role`` of
  ``user`` / ``assistant`` / ``developer``; shell runs through the ``exec``
  ``custom_tool_call`` (a JS script whose ``tools.exec_command({cmd: "…"})``
  calls carry the actual command) and is echoed as ``event_msg`` /
  ``item_completed`` items (``CommandExecution``, ``FileChange``,
  ``McpToolCall``, …). ``event_msg`` ``user_message`` is gone, so an adapter
  that only knew the legacy dialect saw zero human turns and dropped every
  session.

Both dialects are handled at once — a rollout written across a Codex upgrade
mixes them — and turns are de-duplicated across the two channels, so a session
that reports the same text as an ``event_msg`` *and* a ``response_item`` counts
it once. Codex also injects synthetic ``user``-role messages (environment
context, user instructions, AGENTS.md, turn-aborted notices); those are never
human turns and must not be counted as one.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

from ..schema import Record, empty_summary, make_sid
from ._common import Walker, iter_jsonl

NAME = "codex"

# Originators that mean "not a human at a terminal". Compared after normalizing
# whitespace/underscores to hyphens and lowercasing, so ``codex_exec``,
# ``Codex Exec`` and ``codex-exec`` are one entry.
DENIED_ORIGINATORS = frozenset({
    "codex-exec",
    "codex-subagent",
    "codex-mcp",
    "codex-cloud",
    "codex-automation",
})

# Archiving a thread moves its rollout here — a flat dir of the same rollout
# files, a sibling of ``sessions/``.
ARCHIVE_DIRNAME = "archived_sessions"

_NORMALIZE_RX = re.compile(r"[\s_]+")

# Content-part kinds that carry text in a current-format ``message`` item.
# ``input_image`` and friends are ignored.
TEXT_PART_TYPES = frozenset({"input_text", "output_text", "text", "summary_text"})

# Synthetic ``user``-role messages Codex writes on the user's behalf. Anything
# wrapped in a tag (``<environment_context>``, ``<user_instructions>``,
# ``<recommended_plugins>``, ``<turn_aborted>``, ``<in-app-browser-context>``,
# ``<subagent_notification>``, ``<image …>``) is already rejected by
# ``Walker.add_user``; these are the untagged ones. Compared lowercased against
# the left-stripped text.
INJECTED_USER_PREFIXES = (
    "# agents.md instructions",
)

# ``tools.exec_command({cmd: "…"})`` inside the JS script of an ``exec``
# custom_tool_call — the only place the current format spells the shell command
# out at call time.
_JS_CMD_RXS = (
    re.compile(r'\bcmd\s*:\s*"((?:[^"\\]|\\.)*)"'),
    re.compile(r"\bcmd\s*:\s*'((?:[^'\\]|\\.)*)'"),
)

# argv[1] of a login-shell wrapper: ["/bin/zsh", "-lc", "<the real command>"].
_SHELL_FLAGS = frozenset({"-c", "-lc", "-ic", "-lic", "-cl"})


def discover_sources(config) -> list[Path]:
    """``sessions/`` plus its ``archived_sessions/`` sibling when present.

    Archiving is a move, not a copy: without the second root, archiving a thread
    in the desktop app would make its session vanish from the index."""
    src = config.sources.get(NAME)
    if not src or not src.enabled or not src.transcript_dir:
        return []
    root = Path(src.transcript_dir)
    roots = [root] if root.is_dir() else []
    archive = root.parent / ARCHIVE_DIRNAME
    if archive.is_dir() and archive != root:
        roots.append(archive)
    return roots


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


def _originator_denied(value) -> bool:
    """True when this originator means a machine, not a person, drove the thread.
    An empty/absent originator is rejected too — every real client sets one, so a
    blank is a malformed or synthetic rollout, not a new client."""
    if not isinstance(value, str):
        return True
    norm = _NORMALIZE_RX.sub("-", value.strip()).lower()
    if not norm:
        return True
    return norm in DENIED_ORIGINATORS


def _read_meta(path):
    """Read + validate line 1. Returns the session_meta payload dict for an
    interactive top-level thread, else None."""
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
    if _originator_denied(mp.get("originator")):
        return None
    if mp.get("thread_source") != "user":
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
    seen = _SeenTurns()
    with open(path, "r", errors="replace") as f:
        f.readline()  # skip session_meta
        for obj in _iter_body(f, w):
            _handle(w, obj, seen)

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
        client=mp.get("originator") or None,
    )
    rec.excerpt = fields["excerpt"]
    rec.excerpt_full = fields["excerpt_full"]
    rec.turn_count = fields["turn_count"]
    rec.boundaries = fields["boundaries"]
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


class _SeenTurns:
    """Cross-channel turn de-duplication for one rollout.

    A rollout written across a Codex upgrade can report the same turn twice —
    once on the legacy ``event_msg`` channel and once as a current-format
    ``response_item`` message. The first channel to report a given text wins;
    a repeat from the *same* channel is kept, because a human really can send
    "wait" twice."""

    def __init__(self):
        self._channel = {}

    def first_time(self, channel, role, text) -> bool:
        if not isinstance(text, str):
            return True
        key = (role, " ".join(text.split()))
        prev = self._channel.get(key)
        if prev is None:
            self._channel[key] = channel
            return True
        return prev == channel


def _handle(w: Walker, obj, seen: _SeenTurns | None = None):
    w.tick()
    if seen is None:
        seen = _SeenTurns()
    t = obj.get("type")

    if t == "compacted":
        # Codex writes this where it dropped history. Handled before the payload
        # check because the line carries no payload dict of its own.
        w.mark_boundary("precompact", "auto")
        return

    payload = obj.get("payload")
    if not isinstance(payload, dict):
        return
    pt = payload.get("type")

    if t == "turn_context":
        w.set_model(payload.get("model"))
        return

    if t == "event_msg":
        if pt == "user_message":
            _add_user(w, seen, "event_msg", payload.get("message"), obj.get("timestamp"))
        elif pt == "agent_message":
            _add_assistant(w, seen, "event_msg", payload.get("message"))
        elif pt == "patch_apply_end":
            if payload.get("success"):
                w.add_patch_changes(payload.get("changes"))
        elif pt == "mcp_tool_call_end":
            inv = payload.get("invocation")
            if isinstance(inv, dict):
                w.add_mcp(inv.get("server"))
        elif pt == "item_completed":
            _handle_item(w, payload.get("item"))
        return

    if t == "response_item":
        if pt == "message":
            _handle_message(w, seen, obj, payload)
        elif pt == "custom_tool_call":
            _handle_custom_tool_call(w, payload)
        elif pt == "custom_tool_call_output":
            out = _join_text_parts(payload.get("output"))
            if out:
                w.add_command_output(out, call_id=payload.get("call_id"))
        elif pt == "function_call":
            _handle_function_call(w, payload)
        elif pt == "function_call_output":
            out = payload.get("output")
            if not isinstance(out, str):
                out = _join_text_parts(out)
            if out:
                w.add_command_output(out, call_id=payload.get("call_id"))
    # Unknown types ignored (T-007 rule 2).


def _add_user(w, seen, channel, text, ts):
    if not isinstance(text, str) or _is_injected_user_text(text):
        return
    if not seen.first_time(channel, "USER", text):
        return
    w.add_user(text, ts=ts)


def _add_assistant(w, seen, channel, text):
    if not isinstance(text, str) or not text.strip():
        return
    if not seen.first_time(channel, "ASSISTANT", text):
        return
    w.add_assistant(text)


def _join_text_parts(content):
    """Text of a current-format ``content`` / ``output`` value: a list of typed
    parts, or already a plain string."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict) and part.get("type") in TEXT_PART_TYPES:
            txt = part.get("text")
            if isinstance(txt, str):
                parts.append(txt)
    return "\n".join(p for p in parts if p)


def _is_injected_user_text(text) -> bool:
    """True for the ``user``-role messages Codex writes on the user's behalf.

    Tagged injections (``<environment_context>`` &c.) are rejected here as well
    as by ``Walker.add_user``, so they never occupy a de-dup slot."""
    if not isinstance(text, str):
        return True
    stripped = text.lstrip()
    if not stripped or stripped.startswith("<"):
        return True
    low = stripped.lower()
    return any(low.startswith(p) for p in INJECTED_USER_PREFIXES)


def _handle_message(w: Walker, seen, obj, payload):
    """Current-format ``response_item`` / ``message``. ``developer`` and
    ``system`` roles are harness-injected and never a turn."""
    role = payload.get("role")
    if role not in ("user", "assistant"):
        return
    text = _join_text_parts(payload.get("content"))
    if role == "user":
        _add_user(w, seen, "response_item", text, obj.get("timestamp"))
    else:
        _add_assistant(w, seen, "response_item", text)


def _handle_custom_tool_call(w: Walker, payload):
    """The current format's ``exec`` tool: a JS script that calls
    ``tools.exec_command({cmd: "…"})`` one or more times. Only the shell
    commands are harvested; the script scaffolding is not a command."""
    script = payload.get("input")
    if not isinstance(script, str) or not script:
        return
    call_id = payload.get("call_id")
    first = True
    for rx in _JS_CMD_RXS:
        for m in rx.finditer(script):
            cmd = _js_unescape(m.group(1))
            if not cmd:
                continue
            # Only the first command claims the call_id, so the paired
            # custom_tool_call_output is attributed to exactly one command.
            w.add_command(cmd, call_id=call_id if first else None)
            first = False


def _js_unescape(s):
    try:
        return json.loads('"' + s.replace("\n", "\\n") + '"')
    except Exception:
        return s


def _handle_item(w: Walker, item):
    """``event_msg`` / ``item_completed`` — the current format's structured echo
    of a completed transcript item. Turns are deliberately NOT taken from here:
    ``UserMessage`` / ``AgentMessage`` items duplicate the ``response_item``
    messages, which carry the authoritative timestamps."""
    if not isinstance(item, dict):
        return
    kind = item.get("type")
    iid = item.get("id")

    if kind == "CommandExecution":
        cmd = _argv_to_command(item.get("command"))
        if cmd:
            w.add_command(cmd, call_id=iid)
        out = "\n".join(
            s for s in (item.get("stdout"), item.get("stderr"))
            if isinstance(s, str) and s
        )
        if out:
            w.add_command_output(out, call_id=iid)
    elif kind == "FileChange":
        w.add_patch_changes(item.get("changes"))
    elif kind == "McpToolCall":
        w.add_mcp(item.get("server"))
    elif kind == "CollabAgentToolCall":
        agents = item.get("receiver_agents")
        if isinstance(agents, list) and agents:
            for agent in agents:
                if isinstance(agent, dict):
                    w.add_subagent(agent.get("agent_nickname") or "agent")
        elif item.get("tool") == "spawn_agent":
            w.add_subagent("agent")
    elif kind == "Plan":
        _set_plan_todos(w, item.get("plan") or item.get("steps"))


def _argv_to_command(command):
    """``["/bin/zsh", "-lc", "git status"]`` -> ``git status``."""
    if isinstance(command, str):
        return command
    if not isinstance(command, list) or not command:
        return None
    argv = [c for c in command if isinstance(c, str)]
    if len(argv) >= 3 and argv[1] in _SHELL_FLAGS:
        return argv[2]
    return " ".join(argv) or None


def _set_plan_todos(w: Walker, plan):
    if not isinstance(plan, list):
        return
    todos = []
    for step in plan:
        if isinstance(step, dict):
            todos.append({
                "content": step.get("step") or step.get("content") or step.get("text"),
                "status": step.get("status"),
            })
    if todos:
        w.set_todos(todos)


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


def watermark_key(path) -> str:
    """Identity of a rollout for the watermark scan: its uuid, not its path.

    Archiving moves the file, and a path-keyed watermark would read a move as a
    brand-new transcript and re-extract the whole session. The uuid is stable
    across the move (and is the sid uuid), so the size check that follows sees
    an unchanged file and skips it."""
    return f"{NAME}:{_uuid_from_name(Path(path).name)}"


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
