"""Adapter registry.

Each harness is one module exposing the four-function contract
(``NAME``, ``discover_sources``, ``enumerate_sessions``, ``extract``). Third
parties add a module and one entry here; nothing else in the core needs to
change. See ``docs/adapter-contract.md``.
"""

from __future__ import annotations

from . import claude, codex

# Ordered: registry key -> adapter module.
ADAPTERS = {
    claude.NAME: claude,
    codex.NAME: codex,
}

__all__ = ["ADAPTERS"]
