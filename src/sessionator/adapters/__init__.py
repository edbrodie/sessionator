"""Adapter registry.

Each harness is one module exposing the four-function contract
(``NAME``, ``discover_sources``, ``enumerate_sessions``, ``extract``), plus the
optional ``watermark_key``. Third parties add a module and one entry here;
nothing else in the core needs to change. See ``docs/adapter-contract.md``.
"""

from __future__ import annotations

from pathlib import Path

from . import claude, codex

# Ordered: registry key -> adapter module.
ADAPTERS = {
    claude.NAME: claude,
    codex.NAME: codex,
}


def adapter_for_path(config, path):
    """The adapter that owns ``path``, or None when no adapter recognizes it.

    This is how a hook payload's ``transcript_path`` becomes an adapter without
    trusting the harness's own session id (Codex's is fork-shared, so it cannot
    identify one rollout). Recognition is by shape, cheapest test first:

    * ``rollout-*.jsonl`` — a Codex rollout, wherever it lives (``sessions/``,
      ``archived_sessions/``, or a copy);
    * a ``.jsonl`` under the configured Claude projects dir, or directly inside a
      Claude-mangled cwd folder (``-Users-ed-proj``) — a Claude Code session.

    Anything else returns None: an unknown file is not ingested by guesswork.
    """
    p = Path(path)
    name = p.name
    if name.startswith("rollout-") and name.endswith(".jsonl"):
        return ADAPTERS.get(codex.NAME)
    if not name.endswith(".jsonl"):
        return None
    if _under_claude_projects(config, p) or p.parent.name.startswith("-"):
        return ADAPTERS.get(claude.NAME)
    return None


def _under_claude_projects(config, path: Path) -> bool:
    src = getattr(config, "sources", {}).get(claude.NAME) if config else None
    root = getattr(src, "transcript_dir", None) if src else None
    if not root:
        return False
    try:
        path.resolve().relative_to(Path(root).resolve())
    except (ValueError, OSError):
        return False
    return True


__all__ = ["ADAPTERS", "adapter_for_path"]
