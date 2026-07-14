"""Resume-string construction (T-002 resolution).

Each harness gets the *canonical* invocation its own CLI documents, run from the
session's directory so it lands in the right project:

* Claude — ``cd <cwd> && claude --resume <native_id>``
* Codex  — ``cd <cwd> && codex resume <native_id>``

``native_id`` is the harness's verbatim session id (a UUID for both), which is
exactly what ``claude --resume``/``codex resume`` accept as a positional session
id. The line is single, pipeable (``| pbcopy``), and homegrown-syntax-free.
"""

from __future__ import annotations

import shlex

from .schema import Record

_TEMPLATES = {
    "claude": "cd {cwd} && claude --resume {id}",
    "codex": "cd {cwd} && codex resume {id}",
}


def resume_string(rec: Record) -> str:
    """The one-line resume invocation for ``rec``. Unknown harnesses fall back to
    the Claude form (the only two harnesses are claude/codex)."""
    template = _TEMPLATES.get(rec.harness, _TEMPLATES["claude"])
    cwd = shlex.quote(rec.cwd or ".")
    native = shlex.quote(rec.native_id or "")
    return template.format(cwd=cwd, id=native)
