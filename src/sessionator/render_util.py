"""Tiny presentation helpers shared by ``render`` and the ``show`` command."""

from __future__ import annotations

import re

_HARNESS_LABELS = {"claude": "Claude", "codex": "Codex"}


def harness_label(harness: str) -> str:
    return _HARNESS_LABELS.get(harness, (harness or "?").capitalize())


def cwd_tail(cwd: str | None, n: int = 2) -> str:
    """Last ``n`` path segments of a cwd ('?' when unknown)."""
    if not cwd:
        return "?"
    segs = [s for s in cwd.rstrip("/").split("/") if s]
    return "/".join(segs[-n:]) if segs else "?"


def trunc(s: str | None, n: int) -> str:
    s = re.sub(r"\s+", " ", (s or "")).strip()
    return s if len(s) <= n else s[:n].rstrip() + "…"
