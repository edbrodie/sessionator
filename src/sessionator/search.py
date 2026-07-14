"""Query API (T-008 ranking recipe).

``search`` is the one entry point. It loads the records (the store is the output
source — the index only ranks), brings the index current, then:

1. **Text** — free terms are AND-matched in ``fts`` and ``fts_tri``; the two
   best-first lists are fused with **Reciprocal Rank Fusion (k=60)**.
2. **Recency** — each candidate's fused score is multiplied by
   ``0.5^(age_days/90)`` (90-day half-life). Decay orders results; it never
   excludes (that is what ``--since/--until`` are for).
3. **Filters** — ``keyword/repo/cwd/model/harness/since/until/resolved`` are a
   single predicate applied to the candidate records.
4. **Limit** — top N by score, ties broken by ``last_active`` then ``sid``.

A term-less query scores every record 1.0 and lets decay order them (a "recent
sessions" view). Records with pending summaries still match on their
deterministic fields. When FTS5 is unavailable the whole thing runs as a linear
scan behind this same signature.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .index import Index, haystack
from .schema import Record
from .store import Store

RRF_K = 60
HALF_LIFE_DAYS = 90.0


@dataclass
class Filters:
    keyword: list[str] = field(default_factory=list)
    repo: str | None = None
    cwd: str | None = None
    model: str | None = None
    harness: str | None = None
    since: str | None = None
    until: str | None = None
    resolved: str | None = None


def search(
    config,
    terms: list[str] | None = None,
    filters: Filters | None = None,
    *,
    limit: int = 20,
    records: dict[str, Record] | None = None,
    _today: date | None = None,
) -> list[Record]:
    """Ranked, filtered records (best first). ``records`` may be passed to reuse
    an already-loaded store; otherwise it is loaded here."""
    terms = [t for t in (terms or []) if t]
    filters = filters or Filters()
    if records is None:
        records = Store(config).load()
    today = _today or date.today()

    idx = Index(config).ensure_current(records)
    if idx.has_fts:
        base = _fts_scores(idx, terms, records)
    else:
        base = _linear_scores(terms, records)
    idx.close()

    scored = []
    for sid, base_score in base.items():
        rec = records.get(sid)
        if rec is None or not _passes(rec, filters):
            continue
        scored.append((base_score * _decay(rec.date, today), rec))

    scored.sort(key=lambda sr: (sr[0], sr[1].last_active or "", sr[1].sid), reverse=True)
    hits = [rec for _s, rec in scored]
    if limit and limit > 0:
        hits = hits[:limit]
    return hits


# ---------------------------------------------------------------------------
# Text scoring.
# ---------------------------------------------------------------------------

def _fts_scores(idx: Index, terms: list[str], records) -> dict[str, float]:
    """RRF-fused base scores. No terms → every stored sid at 1.0."""
    if not terms:
        return {sid: 1.0 for sid in records}
    porter, tri = idx.fts_rank_lists(terms)
    return _rrf(porter, tri)


def _rrf(*ranklists: list[str]) -> dict[str, float]:
    scores: dict[str, float] = {}
    for ranked in ranklists:
        for pos, sid in enumerate(ranked, start=1):
            scores[sid] = scores.get(sid, 0.0) + 1.0 / (RRF_K + pos)
    return scores


def _linear_scores(terms: list[str], records) -> dict[str, float]:
    """Fallback matcher (no FTS5). AND-substring over the same haystack the
    trigram column uses; matches score 1.0 and are ordered purely by decay."""
    if not terms:
        return {sid: 1.0 for sid in records}
    needles = [t.lower() for t in terms]
    out = {}
    for sid, rec in records.items():
        hs = haystack(rec)
        if all(n in hs for n in needles):
            out[sid] = 1.0
    return out


# ---------------------------------------------------------------------------
# Recency decay.
# ---------------------------------------------------------------------------

def _decay(rec_date: str, today: date) -> float:
    age = _age_days(rec_date, today)
    return 0.5 ** (age / HALF_LIFE_DAYS)


def _age_days(rec_date: str, today: date) -> float:
    try:
        y, m, d = (int(x) for x in (rec_date or "").split("-")[:3])
        age = (today - date(y, m, d)).days
    except (ValueError, TypeError):
        return 0.0
    return float(age) if age > 0 else 0.0


# ---------------------------------------------------------------------------
# Filter predicate — one implementation, shared by both search paths.
# ---------------------------------------------------------------------------

def _passes(rec: Record, f: Filters) -> bool:
    if f.keyword:
        rec_kws = {str(k).lower() for k in (rec.keywords or [])}
        if any(k.lower() not in rec_kws for k in f.keyword):
            return False
    if f.repo and not _substr(rec.repo, f.repo):
        return False
    if f.cwd and not _substr(rec.cwd, f.cwd):
        return False
    if f.model and not _substr(rec.model, f.model):
        return False
    if f.harness and (rec.harness or "") != f.harness:
        return False
    if f.resolved and (rec.resolved or "unknown") != f.resolved:
        return False
    d = rec.date or ""
    if f.since and (not d or d < f.since):
        return False
    if f.until and (not d or d > f.until):
        return False
    return True


def _substr(value: str | None, needle: str) -> bool:
    return isinstance(value, str) and needle.lower() in value.lower()
