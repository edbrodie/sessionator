"""Session lookup by sid-prefix — shared by ``show`` and ``resume``.

Match is prefix-first on the uniform ``<harness>/<uuid>`` sid, falling back to a
substring match so a bare uuid (or a compact-view handle like ``claude/d78b1004``)
resolves. Ambiguity is returned to the caller, not resolved here.
"""

from __future__ import annotations

from .schema import Record


def find_by_prefix(records: dict[str, Record], prefix: str) -> list[Record]:
    """Records whose sid matches ``prefix``. Prefix hits win outright; only when
    there are none do substring hits count. The returned list is the caller's
    ambiguity signal (0 = none, 1 = unique, >1 = ambiguous), ordered by
    date/last_active desc for stable display."""
    needle = prefix.lstrip("…")
    hits = [r for r in records.values() if r.sid.startswith(needle)]
    if not hits:
        hits = [r for r in records.values() if needle in r.sid]
    hits.sort(key=lambda r: (r.date or "", r.last_active or ""), reverse=True)
    return hits
