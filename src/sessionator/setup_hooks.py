"""`sessionator setup codex` — install the Codex hooks that drive capture.

Codex capture is installed separately from the shared skills plugin, in:
``$CODEX_HOME/hooks.json`` (default ``~/.codex/hooks.json``). This module writes
exactly two handlers into it and nothing else:

* ``SessionEnd`` — synchronous, 3 s (Codex's maximum), no matcher. Fires on
  archive, delete, close, and 30 min idle, which is what makes "Codex sessions
  never end" a solved problem.
* ``PreCompact`` — ``async: true``, 600 s, matcher ``*``. Cutting a summary
  segment before history is dropped is the whole point of the segment trail.

Both run ``<abs sessionator> ingest --hook``, which spools and returns in
milliseconds; the timeouts are headroom, not expected cost.

Three rules govern every write, because this file belongs to the user:

1. **Ownership is the exact command string.** No marker keys, no comments, no
   ``_sessionator: true`` — a hook entry whose ``command`` is ours is ours, and
   everything else in the file is untouchable. That also means the user can
   disown a handler simply by editing its command.
2. **Merge, never replace.** Our handler is rewritten in place when present, so
   the positional trust hash Codex computes (``<file>:<event>:<group>:<hook>``)
   survives; otherwise a new group is appended at the END of the event's list, so
   no foreign group's position — and no foreign trust hash — moves.
3. **``config.toml`` is never written.** The user's own hooks live there. It is
   read only as text, only by ``setup status``, only to count foreign hook
   tables.

A corrupt ``hooks.json`` is refused rather than overwritten: it is more likely to
be a hand-edit in progress than a file worth discarding.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path

HOOKS_FILENAME = "hooks.json"
DESCRIPTION = "Codex hooks installed by sessionator (sessionator setup codex)"

# Codex caps a synchronous SessionEnd hook at 3 s; PreCompact may run async with
# a long ceiling. Neither is a cost estimate — `ingest --hook` returns in ms.
SESSION_END_TIMEOUT = 3
PRE_COMPACT_TIMEOUT = 600

# Foreign hook tables in config.toml, counted (never touched) by `setup status`.
_CONFIG_HOOK_TABLE_RX = re.compile(r"^\s*\[\[hooks\.")

TRUST_INSTRUCTIONS = (
    "Next: run /hooks in the Codex CLI and trust the new or changed entries.\n"
    "Trust covers the exact hook definition; changed handlers need review.\n"
    "Trust is positional (<file>:<event>:<group>:<hook>), so reordering the\n"
    "groups in hooks.json by hand invalidates it and you will be asked again."
)


class SetupError(Exception):
    """A condition the user must resolve; the CLI turns this into exit 2."""


@dataclass
class MergeResult:
    data: dict
    changes: list[str] = field(default_factory=list)
    created: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.changes)


# ---------------------------------------------------------------------------
# paths and the command string
# ---------------------------------------------------------------------------

def codex_home() -> Path:
    v = os.environ.get("CODEX_HOME")
    return Path(v) if v else Path.home() / ".codex"


def hooks_path() -> Path:
    return codex_home() / HOOKS_FILENAME


def config_toml_path() -> Path:
    return codex_home() / "config.toml"


def resolve_cli() -> str:
    """The absolute ``sessionator`` a hook should run.

    A hook runs with the harness's environment, not the user's shell, so a bare
    ``sessionator`` on ``PATH`` is not good enough — the path must be absolute
    and stable. ``which`` first (the installed entry point), then this process's
    own argv[0] when it is one. Never a relative path, never ``python -m``: a
    hook that resolves differently later is a hook that silently stops working.
    """
    found = shutil.which("sessionator")
    if found:
        return str(Path(found).resolve())
    argv0 = Path(sys.argv[0] or "")
    if argv0.name == "sessionator":
        try:
            resolved = argv0.resolve()
        except OSError:
            resolved = argv0
        if resolved.exists():
            return str(resolved)
    raise SetupError(
        "no `sessionator` executable found on PATH.\n"
        "Install it first, then re-run this command:\n"
        "  uv tool install git+https://github.com/edbrodie/sessionator\n"
        "(or `pipx install`, or `pip install --user`)"
    )


def hook_command(cli_path: str) -> str:
    return f"{shlex.quote(cli_path)} ingest --hook"


def _owns_command(value, command: str) -> bool:
    """Also recognise the exact unquoted command emitted before path quoting.

    This narrowly migrates our old broken handler without leaving a duplicate.
    Custom commands remain foreign.
    """
    if value == command:
        return True
    parts = shlex.split(command)
    return len(parts) == 3 and value == f"{parts[0]} ingest --hook"


def desired_handlers(command: str) -> dict:
    """The exact hook entries this tool owns, keyed by event."""
    return {
        "SessionEnd": {
            "type": "command",
            "command": command,
            "timeout": SESSION_END_TIMEOUT,
        },
        "PreCompact": {
            "type": "command",
            "command": command,
            "async": True,
            "timeout": PRE_COMPACT_TIMEOUT,
        },
    }


def _group_for(event: str, handler: dict) -> dict:
    """The group wrapper for one handler. SessionEnd takes no matcher (it has
    nothing to match on); PreCompact matches every trigger."""
    group = {"hooks": [handler]}
    if event == "PreCompact":
        group = {"matcher": "*", "hooks": [handler]}
    return group


# ---------------------------------------------------------------------------
# read / merge / unmerge
# ---------------------------------------------------------------------------

def read_hooks(path: Path) -> dict | None:
    """The parsed hooks file, or None when it does not exist. Raises SetupError
    on anything unparseable — a file we cannot understand is a file we must not
    rewrite."""
    if not path.exists():
        return None
    try:
        text = path.read_text(errors="replace")
    except OSError as e:
        raise SetupError(f"cannot read {path}: {e}") from None
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except Exception as e:
        raise SetupError(
            f"{path} is not valid JSON ({e}).\n"
            "Refusing to overwrite it — fix or move the file, then re-run."
        ) from None
    if not isinstance(data, dict):
        raise SetupError(
            f"{path} is valid JSON but not an object. Refusing to overwrite it."
        )
    return data


def _events(data: dict) -> dict:
    hooks = data.get("hooks")
    return hooks if isinstance(hooks, dict) else {}


def _iter_handlers(groups):
    """(group_index, hook_index, handler) over a well-shaped event list."""
    if not isinstance(groups, list):
        return
    for gi, group in enumerate(groups):
        if not isinstance(group, dict):
            continue
        hooks = group.get("hooks")
        if not isinstance(hooks, list):
            continue
        for hi, handler in enumerate(hooks):
            if isinstance(handler, dict):
                yield gi, hi, handler


def find_ours(data: dict, command: str) -> dict:
    """``{event: [(group_index, hook_index)]}`` for handlers whose command is
    exactly ours. Exact string match is the entire ownership test."""
    out = {}
    for event, groups in _events(data).items():
        hits = [
            (gi, hi)
            for gi, hi, handler in _iter_handlers(groups)
            if _owns_command(handler.get("command"), command)
        ]
        if hits:
            out[event] = hits
    return out


def merge(existing: dict | None, command: str) -> MergeResult:
    """Fold our two handlers into ``existing`` (None = create). Idempotent: a
    file already carrying both, unchanged, produces no changes at all."""
    created = existing is None
    data = json.loads(json.dumps(existing)) if existing else {}
    result = MergeResult(data=data, created=created)

    if created or not isinstance(data.get("hooks"), dict):
        if created:
            # A description is ours to set only on a file we are creating.
            data.setdefault("description", DESCRIPTION)
        if not isinstance(data.get("hooks"), dict):
            data["hooks"] = {}

    for event, handler in desired_handlers(command).items():
        groups = data["hooks"].get(event)
        if not isinstance(groups, list):
            groups = []
            data["hooks"][event] = groups

        owned = [
            (gi, hi)
            for gi, hi, h in _iter_handlers(groups)
            if _owns_command(h.get("command"), command)
        ]
        if owned:
            # Rewrite in place: the trust hash is positional, so moving our
            # handler would cost the user a fresh /hooks approval.
            gi, hi = owned[0]
            if groups[gi]["hooks"][hi] != handler:
                groups[gi]["hooks"][hi] = handler
                result.changes.append(f"{event}: updated the sessionator hook")
            for gi, hi in reversed(owned[1:]):
                del groups[gi]["hooks"][hi]
                result.changes.append(f"{event}: removed a duplicate sessionator hook")
        else:
            # Append at the end so no foreign group's position — and no foreign
            # trust hash — shifts.
            groups.append(_group_for(event, handler))
            result.changes.append(f"{event}: added the sessionator hook")

    return result


def unmerge(existing: dict, command: str) -> MergeResult:
    """Drop only our handlers, then any group and event left empty by that.
    Everything foreign survives, including a group we shared."""
    data = json.loads(json.dumps(existing))
    result = MergeResult(data=data)
    events = data.get("hooks")
    if not isinstance(events, dict):
        return result

    for event in list(events):
        groups = events[event]
        owned = [
            (gi, hi)
            for gi, hi, h in _iter_handlers(groups)
            if _owns_command(h.get("command"), command)
        ]
        for gi, hi in reversed(owned):
            del groups[gi]["hooks"][hi]
            result.changes.append(f"{event}: removed the sessionator hook")
        if not owned:
            continue
        # Only prune what our own removal emptied.
        for gi in reversed(range(len(groups))):
            group = groups[gi]
            if isinstance(group, dict) and group.get("hooks") == []:
                del groups[gi]
        if isinstance(groups, list) and not groups:
            del events[event]

    return result


def is_empty(data: dict) -> bool:
    """True when nothing but our own scaffolding is left — the condition for
    deleting the file instead of leaving an empty husk."""
    leftovers = {k: v for k, v in data.items() if k not in ("description", "hooks")}
    if leftovers:
        return False
    hooks = data.get("hooks")
    return not hooks or (isinstance(hooks, dict) and not hooks)


def write_hooks(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def render(data: dict) -> str:
    return json.dumps(data, indent=2)


# ---------------------------------------------------------------------------
# status inputs (all read-only)
# ---------------------------------------------------------------------------

def count_config_toml_hooks(path: Path | None = None) -> int:
    """Foreign ``[[hooks.*]]`` tables in config.toml, by line scan.

    Deliberately not a TOML parse and deliberately never a write: this file is
    where the user's own hooks live, and `setup` promises not to touch it. A
    count is all `status` needs to say "you have hooks here that are not mine".
    """
    p = path or config_toml_path()
    try:
        text = p.read_text(errors="replace")
    except OSError:
        return 0
    return sum(1 for line in text.splitlines() if _CONFIG_HOOK_TABLE_RX.match(line))


# Where a Claude Code hook could be declared. Plugin hooks are nested one level
# deeper than settings, so the search is depth-capped rather than a full walk of
# a directory that can hold thousands of files.
_CLAUDE_SETTINGS_GLOB = "settings*.json"
_CLAUDE_PLUGIN_GLOB = "plugins/*/*/hooks/hooks.json"
_CLAUDE_PLUGIN_GLOB_FLAT = "plugins/*/hooks/hooks.json"


def find_claude_hooks(home: Path | None = None) -> list[Path]:
    """Files under ``~/.claude`` that mention a sessionator hook command.

    Best-effort and read-only: the plugin owns its own hooks and this is only
    here so `setup status` can say whether the Claude half of capture is wired
    up. A miss is reported as "not found", never as an error.
    """
    root = home or (Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude"))
    hits = []
    for pattern in (
        _CLAUDE_SETTINGS_GLOB, _CLAUDE_PLUGIN_GLOB_FLAT, _CLAUDE_PLUGIN_GLOB,
        "plugins/*/claude-hooks/hooks.json", "plugins/*/*/claude-hooks/hooks.json",
        "plugins/cache/*/*/*/claude-hooks/hooks.json",
    ):
        try:
            candidates = sorted(root.glob(pattern))
        except OSError:
            continue
        for path in candidates:
            try:
                text = path.read_text(errors="replace")
            except OSError:
                continue
            if "sessionator" in text and path not in hits:
                hits.append(path)
    return hits


def spool_depth(config) -> int:
    from .hooks import spool_dir

    d = spool_dir(config)
    try:
        return sum(1 for p in d.iterdir() if p.is_file() and p.suffix == ".json")
    except OSError:
        return 0
