"""Hook ingest: spool the payload, detach a worker, get out of the way.

Both harnesses run their hooks synchronously on a budget measured in seconds
(Codex `SessionEnd` is 1 s default / 3 s max), and Claude Code counts the hook's
stdout as part of its protocol. So the foreground half of `sessionator ingest
--hook` does exactly three things and nothing else:

1. read stdin,
2. write it to ``<data_dir>/spool/<time_ns>-<pid>.json`` atomically,
3. ``Popen`` a detached ``python -m sessionator _hook_worker <spool>``,

then returns 0 — silently, always, even when every one of those fails. A capture
tool that can break the user's session is worse than one that misses a session.

The detached worker does the slow half: reconcile that one transcript, cut a
summary segment labelled with the hook event, and run the summarizer over it.
It holds a **non-blocking per-session lock**, so the PreCompact/Stop storm a busy
session produces collapses into one worker instead of a pile-up. Spool files are
unlinked when consumed and reaped after 24 h, so a crashed worker cannot leak
transcript payloads into the data dir indefinitely.

Harness event names are normalized to the segment vocabulary in ``schema.py``:
``PreCompact`` → ``precompact``, ``SessionEnd`` → ``session_end``, and so on;
anything unrecognized is a plain ``change``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# Harness hook_event_name -> the segment ``event`` vocabulary (schema.py).
# A worker is detached, so it can afford to wait for a running sweep to finish
# rather than drop its segment on the floor.
WORKER_BACKFILL_WAIT = 1800.0

EVENT_MAP = {
    "PreCompact": "precompact",
    "PostCompact": "postcompact",
    "SessionEnd": "session_end",
    "Stop": "stop",
    "SessionStart": "session_start",
}
DEFAULT_EVENT = "change"

# Payload keys that carry "why did this hook fire", in precedence order. Claude
# Code uses ``trigger`` (PreCompact) / ``reason`` (SessionEnd) / ``source``
# (SessionStart); Codex uses ``trigger``.
TRIGGER_KEYS = ("trigger", "reason", "source")

SPOOL_TTL_SECONDS = 24 * 60 * 60

_UNSAFE_RX = re.compile(r"[^A-Za-z0-9_.-]+")


# ---------------------------------------------------------------------------
# payload
# ---------------------------------------------------------------------------

def parse_payload(raw) -> dict:
    """The hook's stdin JSON as a dict — ``{}`` for anything unparseable. A hook
    payload is untrusted input from another program's stdout; it never raises."""
    if isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw).decode("utf-8", errors="replace")
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        obj = json.loads(raw)
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


def event_for(payload: dict) -> str:
    """The segment event this hook payload describes."""
    name = payload.get("hook_event_name")
    if not isinstance(name, str):
        return DEFAULT_EVENT
    return EVENT_MAP.get(name.strip(), DEFAULT_EVENT)


def trigger_for(payload: dict) -> str | None:
    """The hook's reason string (``auto``/``manual``/``exit``/``clear``/…)."""
    for key in TRIGGER_KEYS:
        v = payload.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


# ---------------------------------------------------------------------------
# spool + detach (the foreground half)
# ---------------------------------------------------------------------------

def spool_dir(config) -> Path:
    return Path(config.data_dir) / "spool"


def write_spool(config, raw: bytes) -> Path:
    """Persist one payload under the spool dir, atomically. The name carries a
    nanosecond stamp and the pid, so two hooks firing at once cannot collide."""
    d = spool_dir(config)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{time.time_ns()}-{os.getpid()}.json"
    tmp = path.with_suffix(".json.tmp")
    data = raw if isinstance(raw, (bytes, bytearray)) else str(raw).encode("utf-8")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return path


def spool_and_detach(raw: bytes) -> Path | None:
    """Spool ``raw`` and hand it to a detached worker. Returns the spool path.

    The caller is inside a harness hook with a sub-second budget: everything here
    is O(one small write + one fork). Any failure is the caller's to swallow.
    """
    from .config import load as load_config

    config = load_config()
    path = write_spool(config, raw)
    args = [
        sys.executable, "-m", "sessionator", "_hook_worker", str(path),
    ]
    try:
        subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except Exception:
        # The payload stays spooled; the next worker's reap will clear it. Losing
        # one segment is acceptable, breaking the user's hook is not.
        pass
    return path


# ---------------------------------------------------------------------------
# the worker (the background half)
# ---------------------------------------------------------------------------

def run_worker(spool_path) -> None:
    """Consume one spooled payload: reconcile its transcript, cut the segment the
    hook describes, summarize it. Never raises, never prints."""
    path = Path(spool_path)
    try:
        raw = path.read_bytes()
    except OSError:
        raw = b""
    payload = parse_payload(raw)

    config = None
    try:
        from .config import load as load_config

        config = load_config()
        _handle(config, payload)
    except Exception:
        pass

    try:
        path.unlink()
    except OSError:
        pass
    if config is not None:
        try:
            _reap_stale_spool(spool_dir(config))
        except Exception:
            pass


def _handle(config, payload: dict) -> None:
    from .reconcile import reconcile, reconcile_one
    from .summarize import backfill

    event = event_for(payload)
    trigger = trigger_for(payload)
    transcript = payload.get("transcript_path")
    transcript = transcript if isinstance(transcript, str) and transcript else None

    lock = _acquire_session_lock(config, payload, transcript)
    if lock is None:
        # Another worker already owns this session — a PreCompact storm, or a
        # Stop immediately followed by SessionEnd. It will see the same (or
        # newer) transcript bytes, so dropping this one loses nothing.
        return
    try:
        if transcript and Path(transcript).exists():
            result = reconcile_one(
                config, transcript, event=event, trigger=trigger,
            )
        else:
            # No transcript to point at (Codex's ``session_id`` is fork-shared,
            # so it cannot identify our record). Fall back to the ordinary
            # watermark-gated sweep: it costs a stat per transcript and finds
            # whatever actually changed.
            result = reconcile(config, kick_backfill=False)

        for sid in result.touched_sids:
            try:
                backfill(config, only_sid=sid, wait=WORKER_BACKFILL_WAIT)
            except Exception:
                continue
    finally:
        try:
            lock.release()
        except Exception:
            pass


def _acquire_session_lock(config, payload: dict, transcript: str | None):
    """A non-blocking lock scoped to this session, or None when it is held."""
    from .locking import FileLock, LockBusy

    try:
        lock = FileLock(str(_lock_path(config, payload, transcript)), blocking=False)
        return lock.acquire()
    except LockBusy:
        return None
    except Exception:
        # A lock we cannot take must not stop the ingest; worst case two workers
        # race, and the store write lock still serializes them.
        return _NullLock()


class _NullLock:
    def release(self):
        return None


def _lock_path(config, payload: dict, transcript: str | None) -> Path:
    """One lockfile per session. The transcript path is the session identity we
    trust (see ``reconcile_one``); the harness ``session_id`` is only a fallback
    for the no-transcript case."""
    if transcript:
        key = Path(transcript).stem
    else:
        sid = payload.get("session_id")
        key = sid if isinstance(sid, str) and sid.strip() else "sweep"
    safe = _UNSAFE_RX.sub("-", key)[:120] or "hook"
    return Path(config.data_dir) / "hooks" / f"{safe}.lock"


def _reap_stale_spool(directory, *, ttl: float = SPOOL_TTL_SECONDS) -> int:
    """Delete spool files older than ``ttl``. A worker that died mid-flight would
    otherwise leave a copy of a hook payload sitting in the data dir forever."""
    d = Path(directory)
    if not d.is_dir():
        return 0
    cutoff = time.time() - ttl
    removed = 0
    for p in d.iterdir():
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except OSError:
            continue
    return removed
