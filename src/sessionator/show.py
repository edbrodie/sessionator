"""``show`` rendering: the full record, an excerpt/transcript tail, and the
resume string last (T-002).

Transcript source order: the pruning-proof excerpt sidecar first, then the live
``transcript_path`` (which a harness may have pruned), then a graceful
``[no transcript available]``. ``--tail`` caps the shown text at N chars.
"""

from __future__ import annotations

from pathlib import Path

from .render import _full_block
from .resume import resume_string
from .schema import Record
from .store import Store

DEFAULT_TAIL = 4000


def render_show(store: Store, rec: Record, tail: int = DEFAULT_TAIL) -> str:
    body = _full_block(rec)
    excerpt = _transcript_text(store, rec, tail)
    return f"{body}\n\n  transcript:\n{_indent(excerpt)}\n\n{resume_string(rec)}"


def _transcript_text(store: Store, rec: Record, tail: int) -> str:
    text = store.read_excerpt(rec)
    source = "excerpt sidecar"
    if not text:
        text = _read_live(rec.transcript_path)
        source = "live transcript"
    if not text:
        return "[no transcript available]"
    trimmed = _tail(text, tail)
    prefix = f"[{source}]\n"
    return prefix + trimmed


def _read_live(path: str) -> str:
    if not path:
        return ""
    p = Path(path)
    if not p.exists():
        return ""
    try:
        return p.read_text(errors="replace")
    except OSError:
        return ""


def _tail(text: str, tail: int) -> str:
    if tail and tail > 0 and len(text) > tail:
        return "…[trimmed]…\n" + text[-tail:]
    return text


def _indent(text: str) -> str:
    return "\n".join("    " + ln for ln in text.splitlines())
