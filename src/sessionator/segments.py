"""Summary segments: the pure arithmetic behind incremental summaries.

A segment is one cut of a session: "turns ``[start, end)`` of the excerpt, seen
when the transcript was ``bytes`` big". Hooks cut at a compaction or a session
end, an un-hooked Codex session is cut at its ``compacted`` markers, and a
never-hooked session gets one whole-excerpt ``backfill`` cut. The summarizer
folds each new segment into the record's rolled-up ``summary``.

Ordinals are **turns, not transcript bytes**: the summarizer never reads the raw
transcript, only the derived, privacy-stripped excerpt, so a byte offset into the
transcript would not address anything it can see. ``bytes`` is kept purely as a
growth marker (transcript ``st_size`` at cut time).

Everything here is pure: no I/O, no config, no clock beyond an explicit ``at``.
The per-segment sidecar those cuts imply is written by
``store.write_segment_excerpt``.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from .schema import SUMMARY_FIELDS, empty_summary

INPUT_CAP = 6000  # chars of one session/segment fed to the summarizer
# Growth-only cuts (no hook event) wait for at least this many new turns. A
# reconcile runs before every query, so without a floor a live session would
# earn one summarizer call per query while it is still being typed into.
MIN_CHANGE_TURNS = 4

# Excerpt turns are written as ``"USER: …"`` / ``"ASSISTANT: …"`` blocks joined
# by a blank line; a turn's own text may contain blank lines, so turns are found
# by their role prefix at line start, not by splitting on the blank line.
_TURN_SPLIT_RX = re.compile(r"(?m)^(?=(?:USER|ASSISTANT): )")


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def cap_text(text: str, cap: int = INPUT_CAP) -> str:
    """Middle-trim ``text`` to ``cap`` chars, keeping the head and the tail."""
    if not text or len(text) <= cap:
        return text or ""
    head = text[: cap * 2 // 3]
    tail = text[-(cap // 3):]
    return head + "\n...[trimmed]...\n" + tail


# --- turn arithmetic --------------------------------------------------------

def split_turns(excerpt: str) -> list[str]:
    """The excerpt's turns, in order. An excerpt with no role prefixes counts as
    a single turn so a slice of it is never empty."""
    if not excerpt or not excerpt.strip():
        return []
    parts = [p.strip("\n") for p in _TURN_SPLIT_RX.split(excerpt)]
    turns = [p for p in parts if p.strip()]
    return turns or [excerpt.strip()]


def count_turns(excerpt: str) -> int:
    return len(split_turns(excerpt))


def slice_turns(turns, start: int, end: int) -> str:
    """Turns ``[start, end)`` rejoined as excerpt text, with both bounds clamped
    into range (a stale ``end`` from a since-trimmed excerpt must not raise)."""
    n = len(turns)
    lo = max(0, min(int(start), n))
    hi = max(lo, min(int(end), n))
    return "\n\n".join(turns[lo:hi])


# --- segment-list queries ---------------------------------------------------

def last_end(segments) -> int:
    """The turn ordinal the last cut reached — the start of the next segment."""
    ends = [int(s.get("end") or 0) for s in segments or [] if isinstance(s, dict)]
    return max(ends) if ends else 0


def last_bytes(segments) -> int:
    """Transcript size at the most recent cut, for growth detection."""
    sizes = [int(s.get("bytes") or 0) for s in segments or [] if isinstance(s, dict)]
    return max(sizes) if sizes else 0


def next_seq(segments) -> int:
    seqs = [int(s.get("seq") or 0) for s in segments or [] if isinstance(s, dict)]
    return (max(seqs) + 1) if seqs else 1


def pending(segments) -> list[dict]:
    return [
        s for s in segments or []
        if isinstance(s, dict) and s.get("state") == "pending"
    ]


def find(segments, seq) -> dict | None:
    for s in segments or []:
        if isinstance(s, dict) and s.get("seq") == seq:
            return s
    return None


# --- cutting ----------------------------------------------------------------

def make_segment(*, seq, event, trigger, start, end, size, at=None) -> dict:
    return {
        "seq": int(seq),
        "event": event,
        "trigger": trigger,
        "start": int(start),
        "end": int(end),
        "bytes": int(size or 0),
        "at": at or now_iso(),
        "state": "pending",
        "summary": None,
    }


def append_segment(
    rec, *, event, trigger=None, turn_count, size, at=None, force=False,
    min_turns=0,
) -> dict | None:
    """Cut a new segment covering ``[last_end, turn_count)`` and append it.

    Returns the new segment, or None when nothing was cut. Debounced: while a
    segment is still waiting on the summarizer, a further cut would fragment the
    session into one LLM call per keystroke-sized growth, so it is skipped unless
    ``force`` (the user asking for a summary now). A forced cut with nothing new
    re-covers the whole excerpt rather than producing an empty segment.
    ``min_turns`` skips a cut that would cover fewer new turns than that (a
    forced cut ignores it).
    """
    segments = rec.summary_segments or []
    if pending(segments) and not force:
        return None
    start = last_end(segments)
    end = int(turn_count)
    if not force and end - start < int(min_turns or 0):
        return None
    if start >= end:
        if not force:
            return None
        start = 0
        if start >= end:
            return None
    seg = make_segment(
        seq=next_seq(segments), event=event, trigger=trigger,
        start=start, end=end, size=size, at=at,
    )
    rec.summary_segments = list(segments) + [seg]
    return seg


def ensure_backfill_segment(rec, *, turn_count, size, at=None) -> dict | None:
    """The single whole-excerpt cut a never-hooked session gets, so a session no
    hook ever saw is still summarized. No-op once the record has any segment."""
    if rec.summary_segments:
        return None
    return append_segment(
        rec, event="backfill", trigger=None, turn_count=turn_count, size=size, at=at,
    )


def apply_boundaries(rec, boundaries, *, turn_count, size, at=None) -> list[dict]:
    """Turn the transcript's own cut points (Codex ``compacted`` markers) into
    segments, then close the tail. Boundaries are ``{event, trigger, turn}`` with
    ``turn`` the turn ordinal at which the cut happened; out-of-order, duplicate
    and already-covered boundaries are ignored.
    """
    added = []
    for b in boundaries or []:
        if not isinstance(b, dict):
            continue
        turn = int(b.get("turn") or 0)
        if turn <= last_end(rec.summary_segments or []) or turn > int(turn_count):
            continue
        seg = append_segment(
            rec, event=b.get("event") or "precompact", trigger=b.get("trigger"),
            turn_count=turn, size=size, at=at, force=True,
        )
        if seg:
            added.append(seg)
    return added


# --- resolution -------------------------------------------------------------

def mark_done(rec, seq, summary) -> bool:
    """Record a segment's own summary. The rolled-up ``rec.summary`` is the
    summarizer's business; this only closes the segment."""
    seg = find(rec.summary_segments, seq)
    if seg is None or seg.get("state") != "pending":
        return False
    seg["state"] = "done"
    merged = empty_summary()
    if isinstance(summary, dict):
        for k in SUMMARY_FIELDS:
            v = summary.get(k)
            if isinstance(v, str):
                merged[k] = v
    seg["summary"] = merged
    return True


def mark_error(rec, seq) -> bool:
    seg = find(rec.summary_segments, seq)
    if seg is None or seg.get("state") != "pending":
        return False
    seg["state"] = "error"
    return True


def rollup_state(rec) -> str:
    """The record-level ``summary_state`` implied by its segments: ``partial``
    while more is coming but something is already shown, else pending/done/error.
    A record with no segments keeps whatever state it has."""
    segments = [s for s in rec.summary_segments or [] if isinstance(s, dict)]
    if not segments:
        return rec.summary_state
    states = [s.get("state") for s in segments]
    has_done = "done" in states
    if "pending" in states:
        return "partial" if has_done else "pending"
    if has_done:
        return "done"
    return "error" if "error" in states else "pending"
