"""Privacy primitives applied at ingest (T-003 resolution).

Four mechanisms, all enforced here so there is exactly one implementation:

* ``strip_private`` — remove ``<private>...</private>`` spans from text at parse
  time, before anything downstream sees it (before keywords, before the excerpt
  sidecar, before the summarizer). Elisions leave a ``[private]`` placeholder.
* ``cwd_excluded`` — decide whether a session's cwd matches a configured
  exclusion glob. Reconcile uses this both to skip extraction and to
  retroactively purge already-ingested records when the config changes.
* ``scrub_files`` — redact ``files``-manifest entries whose path matches an
  exclusion glob, replacing them with ``["scrubbed", "[excluded-path]"]`` so an
  excluded path appears nowhere in the store, not just not-as-cwd (T-012).
* ``scrub_text`` — redact inline path strings matching an exclusion glob from an
  excerpt body, replacing each with ``[excluded-path]``.

The last two share ``path_excluded``, a file-path variant of ``cwd_excluded``:
it relaxes a leading ``**/`` to ``**`` so a rooted fragment still matches when
the separator before it is not a slash. Claude flattens a session's cwd into its
``~/.claude/projects/<dashed-cwd>/`` directory name, turning the ``/`` before the
excluded segment into ``-``; the vault file then reads ``…-private-notes/…``
and must still be caught by the ``**/private-notes/**`` rule.
"""

from __future__ import annotations

import fnmatch
import re

# DOTALL so a span crossing newlines is fully removed; non-greedy so adjacent
# spans do not merge. Case-insensitive on the tag name.
_PRIVATE_RX = re.compile(r"<private>.*?</private>", re.DOTALL | re.IGNORECASE)

_PLACEHOLDER = "[private]"

# Marker left in place of a redacted path (files manifest op + excerpt inline).
EXCLUDED_MARKER = "[excluded-path]"

# Path-like tokens in free text: at least two segments joined by ``/`` so a bare
# word never matches. Kept greedy over ``[\w.\-]`` so a full absolute path (incl.
# the dashed ~/.claude/projects encoding) is captured as one token.
_PATH_TOKEN_RX = re.compile(r"[~/]?[\w.\-]+(?:/[\w.\-]+)+")


def strip_private(text):
    """Replace every ``<private>...</private>`` span with ``[private]``. Returns
    non-str input unchanged (callers pass through arbitrary JSON values)."""
    if not isinstance(text, str) or not text:
        return text
    # Cheap reject: only pay the regex when an opening tag is present.
    if "<private>" not in text.lower():
        return text
    return _PRIVATE_RX.sub(_PLACEHOLDER, text)


def cwd_excluded(cwd, globs) -> bool:
    """True if ``cwd`` matches any exclusion glob. ``**`` in a pattern behaves as
    ``fnmatch`` treats ``*`` (matches path separators too), which is exactly the
    "contains this path segment" semantics the worked example
    (``**/private-notes/**``) needs. Both the cwd and a trailing-slash
    variant are tested so a session whose cwd IS the excluded root (no trailing
    child) also matches."""
    if not cwd:
        return False
    candidates = (cwd, cwd.rstrip("/") + "/")
    for g in globs or []:
        if not g:
            continue
        for c in candidates:
            if fnmatch.fnmatch(c, g):
                return True
    return False


def _glob_variants(glob: str):
    """fnmatch patterns for matching a *path fragment* (not a cwd). The glob as
    written, plus a leading ``**/`` relaxed to ``**`` so the excluded core still
    matches when the separator before it was flattened to ``-`` (see module
    docstring)."""
    yield glob
    if glob.startswith("**/"):
        yield "**" + glob[3:]


def path_excluded(path, globs) -> bool:
    """True if ``path`` matches any exclusion glob, tolerating the dashed
    project-dir flattening. Unlike :func:`cwd_excluded` this is for arbitrary
    file paths that may sit anywhere in a record, not just the session root."""
    if not isinstance(path, str) or not path:
        return False
    for g in globs or []:
        if not g:
            continue
        for pat in _glob_variants(g):
            if fnmatch.fnmatch(path, pat):
                return True
    return False


def scrub_files(files, globs):
    """Redact ``[op, path]`` manifest entries whose path matches an exclusion
    glob. Returns ``(new_files, n_scrubbed)``; a matched entry becomes
    ``["scrubbed", "[excluded-path]"]``. Idempotent — an already-redacted entry
    (or the marker) is left as-is and not counted."""
    if not files:
        return files, 0
    out = []
    n = 0
    for entry in files:
        if isinstance(entry, (list, tuple)) and len(entry) == 2:
            op, path = entry[0], entry[1]
            if op == "scrubbed" or path == EXCLUDED_MARKER:
                out.append(["scrubbed", EXCLUDED_MARKER])
                continue
            if path_excluded(path, globs):
                out.append(["scrubbed", EXCLUDED_MARKER])
                n += 1
                continue
            out.append([op, path])
        else:
            out.append(entry)
    return out, n


def scrub_text(text, globs):
    """Redact inline path tokens matching an exclusion glob from free text (an
    excerpt body). Returns ``(new_text, n_scrubbed)``. Only path-shaped tokens
    are examined, and only those matching a glob are replaced with
    ``[excluded-path]`` — non-excluded paths and prose are untouched. Idempotent:
    the marker holds no ``/`` so it is never re-matched."""
    if not isinstance(text, str) or not text or not globs:
        return text, 0
    count = 0

    def _repl(m):
        nonlocal count
        tok = m.group(0)
        if path_excluded(tok, globs):
            count += 1
            return EXCLUDED_MARKER
        return tok

    out = _PATH_TOKEN_RX.sub(_repl, text)
    return out, count
